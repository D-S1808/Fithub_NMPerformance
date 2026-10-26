#!/usr/bin/env python3.14
"""FitHub backups for macOS. CPython 3.14.9+ (3.14 branch), Docker and restic."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time


class BackupError(Exception):
    pass


def check_runtime():
    """Deployment baseline reviewed on 2026-10-22; keep patch releases updated."""
    version = sys.version_info
    current = (version.major, version.minor, version.micro)
    if (sys.implementation.name != "cpython" or current[:2] != (3, 14)
            or current < (3, 14, 9) or version.releaselevel != "final"):
        raise BackupError(
            "Richiesto CPython stabile del ramo 3.14, versione 3.14.9 o successiva; "
            f"trovato {'.'.join(map(str, current))} ({version.releaselevel}). "
            "Installare/aggiornare python@3.14 e usare scripts/backup/run.sh.")


def require(condition, message):
    if not condition:
        raise BackupError(message)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)


class Backup:
    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser().resolve()
        self.cfg = json.loads(self.config_path.read_text())
        self.root = (self.config_path.parent / Path(self.cfg["root"]).expanduser()).resolve()
        require(self.root.is_dir(), f"Directory FitHub assente: {self.root}")
        self.store = self.root / "backups"
        self.monitor_status_dir = self.root / "scripts" / "monitormetrics"
        self.repo = self.store / "restic"
        self.current = self.store / "database" / "current"
        self.host = self.cfg.get("host", "fithub-mac-mini")
        require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+", self.host)), "host non valido")
        for key in ("keep_daily", "keep_weekly", "keep_monthly", "keep_weekly_bundles"):
            require(type(self.cfg[key]) is int and self.cfg[key] > 0, f"{key} deve essere positivo")
        self.pg = None
        self.log = logging.getLogger("fithub-backup")

    def path(self, value):
        return (self.root / Path(value).expanduser()).resolve()

    def run(self, args, *, capture=False, output=None, input_file=None, env=None, timeout=None):
        # Do not log argv: repository URLs and Docker inspection can contain secrets.
        result = subprocess.run([str(a) for a in args], stdin=input_file,
                                stdout=subprocess.PIPE if capture else output,
                                stderr=None, env=env, timeout=timeout, check=False, cwd=self.root)
        require(result.returncode == 0,
                f"{Path(str(args[0])).name}: comando fallito (codice {result.returncode})")
        return result.stdout.decode() if capture else None

    def ssd_repository(self):
        mount_value = self.cfg.get("ssd_mount")
        require(bool(mount_value), "SSD non configurato: impostare ssd_mount")
        mount = Path(mount_value)
        require(mount.is_absolute() and mount.parent == Path("/Volumes"),
                "ssd_mount deve indicare il volume esatto sotto /Volumes")
        # Do not mkdir the mount point: an absent SSD must never become a local directory.
        require(mount.is_mount(), f"SSD non montato: {mount}")
        relative = Path(self.cfg.get("ssd_repository", "FitHub-backup/restic"))
        require(not relative.is_absolute() and relative.parts and ".." not in relative.parts,
                "ssd_repository deve essere un percorso relativo interno al volume")
        repo = (mount / relative).resolve()
        require(repo.is_relative_to(mount.resolve()), "Repository SSD esterno al volume configurato")
        require(repo != self.repo.resolve(), "Il repository SSD deve essere distinto da quello locale")
        return repo

    def restic(self, *args, cloud=False, ssd=False, capture=False):
        require(not (cloud and ssd), "Scegliere una sola destinazione")
        repo = str(self.repo)
        if cloud:
            repo = self.cfg.get("cloud_repository")
        elif ssd:
            repo = self.ssd_repository()
        require(bool(repo), "Repository cloud non configurato")
        password_key = "ssd_password_file" if ssd else "cloud_password_file" if cloud else "password_file"
        password = self.path(self.cfg[password_key])
        require(password.is_file() and password.stat().st_size > 0,
                f"File password assente/vuoto: {password}")
        require(password.stat().st_mode & 0o077 == 0, f"Usare chmod 600 {password}")
        require(not password.is_relative_to(self.root), "Tenere le password restic fuori da FitHub")
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        if cloud and self.cfg.get("cloud_env_file"):
            env_path = self.path(self.cfg["cloud_env_file"])
            require(env_path.stat().st_mode & 0o077 == 0, "cloud_env_file richiede chmod 600")
            require(not env_path.is_relative_to(self.root), "Tenere le credenziali cloud fuori da FitHub")
            # JSON map, not a shell script; values are never evaluated or printed.
            credentials = json.loads(env_path.read_text())
            require(isinstance(credentials, dict) and all(
                isinstance(k, str) and isinstance(v, str) and not k.startswith("RESTIC_")
                for k, v in credentials.items()), "cloud_env_file deve contenere una mappa JSON di stringhe")
            env.update(credentials)
        return self.run(["restic", "--repo", repo, "--password-file", password, *args],
                        capture=capture, env=env)

    def publish_monitor_status(self, command, record):
        """Export minimal metadata only when the monitor installer prepared its folder."""
        if command not in ("daily", "weekly", "monthly"):
            return
        try:
            if not self.monitor_status_dir.is_dir():
                return
            write_json(self.monitor_status_dir / f"status-{command}.json", {
                "ok": record["ok"], "finished_at": record["finished_at"]})
        except OSError:
            # Backup success/failure must not depend on the monitoring destination.
            self.log.warning("Stato monitoring backup non aggiornato; verificare cartella e permessi")

    @contextlib.contextmanager
    def locked(self, command):
        self.store.mkdir(mode=0o700, exist_ok=True)
        # flock is released by the OS, including on crashes. Never delete this file.
        with (self.store / ".backup.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupError("Un altro backup/verifica è già in esecuzione") from exc
            logs = self.store / "logs" / command
            logs.mkdir(mode=0o700, parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = logs / f"{stamp}-{command}-{os.getpid()}.log"
            # External command output also goes to the private per-run log.
            with log_path.open("a", buffering=1) as stream:
                old_stdout, old_stderr = os.dup(1), os.dup(2)
                sys.stdout.flush()
                sys.stderr.flush()
                try:
                    os.dup2(stream.fileno(), 1)
                    os.dup2(stream.fileno(), 2)
                    self.log.info("Avvio %s", command)
                    yield
                    self.log.info("Completato %s", command)
                    record = {
                        "ok": True, "finished_at": dt.datetime.now().astimezone().isoformat(),
                        "log": str(log_path)}
                    write_json(self.store / f"status-{command}.json", record)
                    self.publish_monitor_status(command, record)
                except BaseException as exc:
                    self.log.error("Fallito %s: %s", command, exc)
                    record = {
                        "ok": False, "finished_at": dt.datetime.now().astimezone().isoformat(),
                        "log": str(log_path), "error": str(exc)}
                    write_json(self.store / f"status-{command}.json", record)
                    self.publish_monitor_status(command, record)
                    raise
                finally:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os.dup2(old_stdout, 1)
                    os.dup2(old_stderr, 2)
                    os.close(old_stdout)
                    os.close(old_stderr)
                    print(f"Log: {log_path}", file=sys.stderr)

    def postgres(self):
        if self.pg:
            return self.pg
        container = self.cfg.get("postgres_container")
        if not container:
            compose = self.path(self.cfg["postgres_compose"])
            require(compose.is_file(), f"Compose PostgreSQL assente: {compose}")
            command = ["docker", "compose"]
            if (self.root / "configs/.env").is_file():
                command += ["--env-file", self.root / "configs/.env"]
            ids = self.run(command + ["-f", compose, "ps", "-q"], capture=True).split()
            require(len(ids) == 1, "Compose deve individuare un container; impostare postgres_container")
            container = ids[0]
        info = json.loads(self.run(["docker", "inspect", container], capture=True))[0]
        require(info["State"]["Running"], "PostgreSQL non è in esecuzione")
        env = dict(v.split("=", 1) for v in info["Config"].get("Env", []) if "=" in v)
        user = self.cfg.get("postgres_user") or env.get("POSTGRES_USER", "postgres")
        self.pg = {"container": info["Id"], "image": info["Image"], "user": user,
                   "mounts": info.get("Mounts", []),
                   "pgdata": env.get("PGDATA", "/var/lib/postgresql/data")}
        return self.pg

    def pg_exec(self, *args, capture=False, output=None, input_file=None, container=None):
        pg = self.postgres()
        # The password stays inside the container, including support for Docker secrets.
        shell = ('if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then '
                 'PGPASSWORD=$(cat "$POSTGRES_PASSWORD_FILE") || exit; '
                 'else PGPASSWORD=${POSTGRES_PASSWORD:-}; fi; '
                 'export PGPASSWORD; exec "$@"')
        return self.run(["docker", "exec", "-i", container or pg["container"],
                         "sh", "-c", shell, "backup", *args], capture=capture,
                        output=output, input_file=input_file)

    def sources(self):
        for item in self.cfg["required_paths"]:
            path = self.path(item)
            require(path.is_relative_to(self.root) and path.exists(), f"Percorso richiesto assente: {path}")
            lexical = self.root / item
            require(not any(p.is_symlink() for p in [lexical, *lexical.parents] if p != self.root),
                    f"Percorso richiesto tramite symlink: configurare un percorso reale ({item})")
        # Enumerate children instead of backing up root: backups and logs can never recurse.
        return sorted(p for p in self.root.iterdir() if p.name not in
                      {"backups", "logs", *self.cfg["exclude_names"]})

    def excluded_paths(self):
        paths = [self.path(p) for p in self.cfg.get("exclude_paths", [])]
        pg = self.postgres()
        for mount in pg["mounts"]:
            dest = Path(mount["Destination"])
            if mount["Type"] == "bind" and Path(pg["pgdata"]).is_relative_to(dest):
                paths.append(Path(mount["Source"]).resolve())
        for required in self.cfg["required_paths"]:
            path = self.path(required)
            require(not any(path.is_relative_to(ex) for ex in paths),
                    f"Un'esclusione copre un percorso richiesto: {path}")
        return paths

    def dump(self, target, kind):
        target.mkdir(mode=0o700, parents=True)
        database_dir = target / "database"
        database_dir.mkdir()
        pg = self.postgres()
        databases = self.cfg.get("databases") or json.loads(self.pg_exec(
            "psql", "-XAt", "--no-password", "--username", pg["user"], "--dbname", "postgres",
            "-c", "SELECT coalesce(json_agg(datname ORDER BY datname), '[]'::json) "
                  "FROM pg_database WHERE NOT datistemplate AND datallowconn", capture=True))
        require(isinstance(databases, list) and databases and all(isinstance(n, str) and n for n in databases),
                "Nessun database selezionato")
        entries = []
        for number, name in enumerate(databases, 1):
            file = database_dir / f"db-{number:03d}.dump"
            self.log.info("Dump database %s", name)
            with file.open("wb") as stream:
                self.pg_exec("pg_dump", "--no-password", "--username", pg["user"],
                             "--dbname", name, "--format=custom", output=stream)
            require(file.stat().st_size > 0, "Dump database vuoto")
            with file.open("rb") as stream:
                self.pg_exec("pg_restore", "--list", input_file=stream, capture=True)
            entries.append({"name": name, "file": str(file.relative_to(target))})
        with (database_dir / "globals.sql").open("wb") as stream:
            self.pg_exec("pg_dumpall", "--no-password", "--username", pg["user"],
                         "--database=postgres", "--globals-only", output=stream)
        require((database_dir / "globals.sql").stat().st_size > 0, "Dump globals vuoto")
        manifest = {"version": 1, "kind": kind,
                    "created_at": dt.datetime.now().astimezone().isoformat(),
                    "root": str(self.root), "postgres_image": pg["image"],
                    "postgres_user": pg["user"], "databases": entries,
                    "required_paths": self.cfg["required_paths"],
                    "exclude_names": self.cfg["exclude_names"],
                    "exclude_paths": [str(p) for p in self.excluded_paths()]}
        write_json(target / "manifest.json", manifest)

    def checksums(self, target):
        files = sorted(p for p in target.rglob("*") if p.is_file() and p.name != "checksums.json")
        write_json(target / "checksums.json", {str(p.relative_to(target)): sha256(p) for p in files})

    def validate(self, target):
        hashes = json.loads((target / "checksums.json").read_text())
        require("manifest.json" in hashes and "database/globals.sql" in hashes, "Checksum incompleti")
        manifest = json.loads((target / "manifest.json").read_text())
        for entry in manifest["databases"]:
            require(entry["file"] in hashes, "Checksum dump assente")
        if manifest["kind"] == "weekly":
            require("application.tar.gz" in hashes, "Checksum archivio applicativo assente")
        for name, expected in hashes.items():
            path = (target / name).resolve()
            require(path.is_relative_to(target.resolve()) and path.is_file(), "File backup mancante/non valido")
            require(sha256(path) == expected, f"Checksum errato: {name}")
        return manifest

    def archive(self, target, sources):
        exclusions = self.excluded_paths()
        def keep(info):
            relative = Path(info.name)
            original = self.root / relative
            if any(part in self.cfg["exclude_names"] for part in relative.parts):
                return None
            if any(original.is_relative_to(ex) for ex in exclusions):
                return None
            require(not info.isdev() and not info.isfifo(), f"File speciale non archiviabile: {relative}")
            return info
        with tarfile.open(target / "application.tar.gz", "w:gz", dereference=False) as archive:
            for source in sources:
                archive.add(source, arcname=source.name, filter=keep)

    def snapshot(self, paths, kind, exclusions=()):
        args = ["backup", "--json", "--host", self.host, "--tag", "fithub", "--tag", kind]
        for name in self.cfg["exclude_names"]:
            args += ["--exclude", name]
        for path in exclusions:
            args += ["--exclude", str(path)]
        output = self.restic(*args, "--", *map(str, paths), capture=True)
        summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
        ids = [s["snapshot_id"] for s in summaries if s.get("message_type") == "summary" and s.get("snapshot_id")]
        require(len(ids) == 1, "Restic non ha confermato lo snapshot")
        self.log.info("Snapshot locale: %s", ids[0])
        return ids[0]

    def sync(self, cloud=False, ssd=False):
        require(not (cloud and ssd), "Scegliere una sola destinazione")
        if cloud:
            destinations = ["cloud"]
        elif ssd:
            destinations = ["ssd"]
        else:
            destinations = []
            if self.cfg.get("cloud_repository"):
                destinations.append("cloud")
            if self.cfg.get("ssd_mount"):
                destinations.append("ssd")
        require(destinations, "Nessuna copia esterna configurata")
        source = json.loads(self.restic("snapshots", "--json", "--host", self.host,
                                        "--tag", "fithub", capture=True))
        require(source, "Nessuno snapshot locale da copiare")
        errors = []
        for destination in destinations:
            options = {destination: True}
            try:
                # Copy complete snapshots, including their exact DB dumps and metadata.
                # ALL retained snapshots are copied to catch up after network/disk outages.
                self.restic("copy", "--from-repo", self.repo, "--from-password-file",
                            self.path(self.cfg["password_file"]), "--host", self.host,
                            "--tag", "fithub", **options)
                self.restic("check", **options)
                copied = json.loads(self.restic("snapshots", "--json", "--host", self.host,
                                               "--tag", "fithub", capture=True, **options))
                for row in source:
                    original = row.get("original") or row["id"]
                    matches = [other for other in copied if
                               (other.get("original") or other["id"]) == original]
                    require(any(other["tree"] == row["tree"] and other["paths"] == row["paths"]
                                for other in matches),
                            f"Copia {destination} incompleta per snapshot {row['id']}")
                self.log.info("Copia %s verificata: %s snapshot completi, inclusi i dump DB",
                              destination, len(source))
            except (BackupError, OSError, ValueError, KeyError) as exc:
                errors.append(destination)
                self.log.error("Copia %s fallita: %s", destination, exc)
        require(not errors, "Copie esterne non completate: " + ", ".join(errors))

    def retention(self, prune):
        destinations = [{}]
        if self.cfg.get("cloud_repository"):
            destinations.append({"cloud": True})
        if self.cfg.get("ssd_mount"):
            destinations.append({"ssd": True})
        for options in destinations:
            for kind in ("daily", "weekly"):
                args = ["forget", "--host", self.host, "--tag", f"fithub,{kind}",
                        "--group-by", "host,tags"]
                if kind == "daily":
                    args += ["--keep-daily", str(self.cfg["keep_daily"])]
                else:
                    args += ["--keep-weekly", str(self.cfg["keep_weekly"]),
                             "--keep-monthly", str(self.cfg["keep_monthly"])]
                self.restic(*args, **options)
            if prune:
                self.restic("prune", **options)
                self.restic("check", **options)

    def backup(self, kind, restore_test=False):
        sources = self.sources()
        self.restic("cat", "config", capture=True)  # Never silently initialize a missing repository.
        self.postgres()
        self.excluded_paths()
        if kind == "weekly":
            parent = self.store / "weekly"
            parent.mkdir(mode=0o700, exist_ok=True)
            target = parent / (dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3))
            self.dump(target, kind)
            self.archive(target, sources)
            self.checksums(target)
            self.validate(target)
            snapshot = self.snapshot([target], kind)
        else:
            self.current.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".daily-", dir=self.current.parent) as temp:
                target = Path(temp) / "current"
                self.dump(target, kind)
                self.checksums(target)
                self.validate(target)
                if self.current.exists():
                    shutil.rmtree(self.current)
                target.rename(self.current)
            snapshot = self.snapshot([*sources, self.current], kind, self.excluded_paths())
        self.restic("check")
        if restore_test:
            self.verify(snapshot, restore_test=True)
        if self.cfg.get("cloud_repository") or self.cfg.get("ssd_mount"):
            self.sync()
        else:
            self.log.warning("Cloud e SSD non configurati: backup SOLO LOCALE")
        self.retention(prune=(kind == "weekly"))
        if kind == "weekly":
            # Only delete completed bundles after the snapshot and all configured external copies.
            bundles = sorted(p for p in target.parent.iterdir() if p.is_dir()
                             and re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{6}", p.name)
                             and (p / ".complete").is_file())
            (target / ".complete").touch()
            bundles.append(target)
            for old in sorted(set(bundles))[:-self.cfg["keep_weekly_bundles"]]:
                shutil.rmtree(old)
        return snapshot

    def verify(self, snapshot="latest", cloud=False, restore_test=False, read_data=False, ssd=False):
        options = {"cloud": cloud, "ssd": ssd}
        self.restic("check", *(["--read-data"] if read_data else []), **options)
        rows = json.loads(self.restic("snapshots", "--json", "--host", self.host,
                                     "--tag", "fithub", *([] if snapshot == "latest" else [snapshot]),
                                     capture=True, **options))
        if snapshot == "latest" and rows:
            rows = [max(rows, key=lambda r: dt.datetime.fromisoformat(r["time"].replace("Z", "+00:00")))]
        require(len(rows) == 1, "Snapshot assente o selezione ambigua")
        row = rows[0]
        kind = "weekly" if "weekly" in row.get("tags", []) else "daily"
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=self.store) as temp:
            restored = Path(temp)
            self.restic("restore", row["id"], "--target", restored, "--verify", **options)
            if kind == "weekly":
                require(len(row["paths"]) == 1, "Percorsi snapshot weekly inattesi")
                relative = Path(row["paths"][0]).relative_to("/")
            else:
                candidates = [Path(p) for p in row["paths"] if p.endswith("/backups/database/current")]
                require(len(candidates) == 1, "Metadata daily non individuati")
                relative = candidates[0].relative_to("/")
            target = restored / relative
            require(target.resolve().is_relative_to(restored.resolve()), "Percorso snapshot non valido")
            manifest = self.validate(target)
            if kind == "weekly":
                with tarfile.open(target / "application.tar.gz", "r:gz") as archive:
                    names = set(archive.getnames())
                    require(all(p in names for p in manifest["required_paths"]), "Copertura archivio incompleta")
            else:
                source_root = restored / Path(manifest["root"]).relative_to("/")
                require(all((source_root / p).exists() for p in manifest["required_paths"]),
                        "Copertura snapshot incompleta")
            # --list uses the source PostgreSQL tools; restore-test works without production running.
            if restore_test:
                self.restore_test(target, manifest)
            else:
                for entry in manifest["databases"]:
                    with (target / entry["file"]).open("rb") as stream:
                        self.pg_exec("pg_restore", "--list", input_file=stream, capture=True)
            self.log.info("Verifica snapshot %s completata (restore DB: %s)", row["id"], restore_test)

    def restore_test(self, target, manifest):
        name = "fithub-restore-test-" + secrets.token_hex(6)
        user = "verify_" + secrets.token_hex(6)
        password = secrets.token_hex(24)
        env = dict(os.environ, POSTGRES_PASSWORD=password)
        created = False
        # Same immutable local image ID as production: no tag/version guessing or network pull.
        try:
            self.run(["docker", "run", "--detach", "--pull=never", "--name", name,
                      "--network", "none", "--label", "ch.nmperformance.fithub-backup-test=true",
                      "-e", "POSTGRES_PASSWORD", "-e", f"POSTGRES_USER={user}",
                      "-e", "POSTGRES_DB=postgres", manifest["postgres_image"]], env=env, capture=True)
            created = True
            for attempt in range(60):
                # The official image's init server accepts Unix sockets before initialization
                # finishes. TCP becomes available only after the final server has started.
                result = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", user,
                                         "-d", "postgres"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise BackupError("PostgreSQL temporaneo non pronto entro 60 secondi")
            def execute(*args, input_file=None, capture=False):
                return self.run(["docker", "exec", "-i", name, *args],
                                input_file=input_file, capture=capture)
            with (target / "database/globals.sql").open("rb") as stream:
                execute("psql", "-X", "--set", "ON_ERROR_STOP=1", "-U", user,
                        "-d", "postgres", input_file=stream)
            for entry in manifest["databases"]:
                database = entry["name"]
                with (target / entry["file"]).open("rb") as stream:
                    execute("pg_restore", "--exit-on-error", "--clean", "--if-exists", "--create",
                            "-U", user, "--dbname", "template1", input_file=stream)
                count = execute("psql", "-XAt", "--set", "ON_ERROR_STOP=1", "-U", user,
                                "--dbname", database, "-c", "SELECT count(*) FROM pg_catalog.pg_tables "
                                "WHERE schemaname NOT IN ('pg_catalog','information_schema')", capture=True)
                self.log.info("Restore riuscito: %s (%s tabelle)", database, count.strip())
        finally:
            # Removes anonymous data volumes too; never touches production volumes.
            if created:
                failed = sys.exc_info()[0] is not None
                try:
                    self.run(["docker", "rm", "--force", "--volumes", name], capture=True)
                except (BackupError, OSError):
                    if not failed:
                        raise
                    self.log.error("Impossibile eliminare il container temporaneo %s", name)

    def monthly(self):
        require(sys.platform == "darwin", "Time Machine richiede macOS")
        destination = self.cfg.get("time_machine_destination_id")
        mount = self.cfg.get("time_machine_mount")
        require(destination and mount, "Configurare time_machine_destination_id e time_machine_mount")
        mount = Path(mount)
        require(mount.is_mount(), "SSD Time Machine non montato")
        text = self.run(["/usr/bin/tmutil", "destinationinfo"], capture=True)
        destinations = [dict((key.strip(), value.strip()) for key, value in
                             (line.split(":", 1) for line in block.splitlines() if ":" in line))
                        for block in re.split(r"=+", text)]
        require(any(d.get("ID", "").strip() == destination and
                    d.get("Mount Point", "").strip() == str(mount) for d in destinations),
                "Destinazione Time Machine diversa dall'SSD atteso")
        # Fresh application backup before the OS-level backup; failure stops Time Machine.
        self.backup("weekly", restore_test=True)
        result = subprocess.run(["/usr/bin/tmutil", "latestbackup", "-d", str(mount)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        before = result.stdout.decode() if result.returncode == 0 else ""
        self.run(["/usr/bin/tmutil", "startbackup", "--block", "--destination", destination])
        after = self.run(["/usr/bin/tmutil", "latestbackup", "-d", mount], capture=True)
        require(after.strip() and after != before, "Time Machine non conferma un nuovo backup completato")
        self.log.info("Time Machine completato. Espellere e scollegare manualmente l'SSD.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init-local", "init-cloud", "init-ssd", "daily", "weekly", "verify", "sync", "monthly"])
    parser.add_argument("--config", default=str(Path(__file__).with_name("backup.json")))
    parser.add_argument("--snapshot", default="latest")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--cloud", action="store_true", help="Verifica/sincronizza solo il repository cloud")
    destination.add_argument("--ssd", action="store_true", help="Verifica/sincronizza solo il repository SSD")
    parser.add_argument("--restore-test", action="store_true", help="Ripristina i DB in un container isolato")
    parser.add_argument("--read-data", action="store_true", help="Legge tutti i blocchi restic durante check")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Allow context managers to clean up the test container on SIGTERM as well as Ctrl-C.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit("Terminato da SIGTERM"))
    try:
        check_runtime()  # Fail before reading deployment config or creating backup files.
        backup = Backup(args.config)
        with backup.locked(args.command):
            if args.command == "init-local":
                backup.restic("init")
            elif args.command in ("init-cloud", "init-ssd"):
                backup.restic("init", "--from-repo", backup.repo, "--from-password-file",
                              backup.path(backup.cfg["password_file"]), "--copy-chunker-params",
                              cloud=args.command == "init-cloud", ssd=args.command == "init-ssd")
            elif args.command in ("daily", "weekly"):
                backup.backup(args.command, restore_test=args.restore_test)
            elif args.command == "verify":
                backup.verify(args.snapshot, args.cloud, args.restore_test, args.read_data, ssd=args.ssd)
            elif args.command == "sync":
                backup.sync(cloud=args.cloud, ssd=args.ssd)
            else:
                backup.monthly()
    except (BackupError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"ERRORE: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
