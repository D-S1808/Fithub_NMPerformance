"""Failure-path and archive tests. No Docker daemon, cloud account or Mac mini needed."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts/backup/backup.py"
spec = importlib.util.spec_from_file_location("backup", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SimulatedBackup(module.Backup):
    """Replace only external Docker/restic boundaries; run actual filesystem logic."""
    def __init__(self, path, vault):
        super().__init__(path)
        self.vault = vault
        self.calls = []
        self.failure = None
        self.external_rows = {}
        self.dump_bytes = b"PGDMP-test-fixture"
        self.pg = {"container": "production-id", "image": "sha256:immutable-image",
                   "user": "fithub", "pgdata": "/var/lib/postgresql/data",
                   "mounts": [{"Type": "bind", "Destination": "/var/lib/postgresql/data",
                               "Source": str(self.root / "raw-postgres")}]}

    def pg_exec(self, *args, **kwargs):
        self.calls.append(("postgres", args))
        if args[0] == "pg_dump":
            kwargs["output"].write(self.dump_bytes)
        elif args[0] == "pg_dumpall":
            kwargs["output"].write(b"CREATE ROLE fithub;\n")
        elif args[0] == "psql":
            return '["fithub", "postgres"]'
        elif self.failure == "dump-list":
            raise module.BackupError("unreadable dump")
        return ""

    def restic(self, *args, cloud=False, ssd=False, capture=False):
        tool = "cloud" if cloud else "ssd" if ssd else "restic"
        self.calls.append((tool, args))
        if args[0] == self.failure or (cloud and self.failure == "cloud") or (ssd and self.failure == "ssd"):
            raise module.BackupError("simulated restic failure")
        if args[0] == "backup":
            paths = list(args[args.index("--") + 1:])
            kind = "weekly" if "weekly" in args else "daily"
            self.snapshot_row = {"id": "a" * 64, "tags": ["fithub", kind], "paths": paths,
                                 "time": "2026-10-22T02:00:00+02:00", "tree": "c" * 64}
            self.vault.mkdir(exist_ok=True)
            exclusions = self.excluded_paths()
            def ignored(parent, names):
                return [n for n in names if n in self.cfg["exclude_names"] or
                        any((Path(parent) / n).is_relative_to(ex) for ex in exclusions)]
            for name in paths:
                source = Path(name)
                if any(source.is_relative_to(ex) for ex in exclusions):
                    continue
                destination = self.vault / source.relative_to("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, destination, ignore=ignored, symlinks=True, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, destination)
            return json.dumps({"message_type": "summary", "snapshot_id": self.snapshot_row["id"]})
        if args[0] == "copy":
            destination = self.vault.parent / tool
            shutil.copytree(self.vault, destination, dirs_exist_ok=True)
            self.external_rows[tool] = dict(self.snapshot_row, original=self.snapshot_row["id"],
                                            id=("b" if cloud else "d") * 64)
            if self.failure == "incomplete-copy":
                self.external_rows[tool]["tree"] = "wrong-tree"
        if args[0] == "snapshots":
            row = self.external_rows.get(tool) if tool != "restic" else getattr(self, "snapshot_row", None)
            return json.dumps([row] if row else [])
        if args[0] == "restore":
            vault = self.vault if tool == "restic" else self.vault.parent / tool
            shutil.copytree(vault, Path(args[args.index("--target") + 1]), dirs_exist_ok=True)
        return "{}"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fithub tests ")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "Fithub"
        self.root.mkdir()
        for directory in ("configs", "FitHub", "library", "scripts/backup", "SQL", "srv/clients",
                          "logs", "backups/clients", "FitHub/frontend/node_modules", "raw-postgres"):
            path = self.root / directory
            path.mkdir(parents=True, exist_ok=True)
            (path / "fixture.txt").write_text(directory)
        (self.root / "configs/.env").write_text("fake config, no credentials")
        cfg = json.loads(MODULE.with_name("backup.example.json").read_text())
        self.config = self.root / "scripts/backup/backup.json"
        self.config.write_text(json.dumps(cfg))
        self.backup = SimulatedBackup(self.config, self.base / "snapshots")

    def tearDown(self):
        self.temp.cleanup()

    def actions(self):
        return [args[0] for tool, args in self.backup.calls if tool in ("restic", "cloud", "ssd")]

    def test_daily_snapshot_contains_all_required_paths_and_all_databases(self):
        self.backup.backup("daily")
        manifest = self.backup.validate(self.backup.current)
        self.assertEqual([e["name"] for e in manifest["databases"]], ["fithub", "postgres"])
        self.backup.verify()
        row = self.backup.snapshot_row
        self.assertIn(str(self.root / "FitHub"), row["paths"])
        self.assertNotIn(str(self.root / "backups"), row["paths"])
        self.assertNotIn(str(self.root / "logs"), row["paths"])

    def test_weekly_bundle_is_complete_and_excludes_raw_data_dependencies_and_backups(self):
        self.backup.backup("weekly")
        bundle = Path(self.backup.snapshot_row["paths"][0])
        with tarfile.open(bundle / "application.tar.gz") as archive:
            names = archive.getnames()
        self.assertIn("configs/.env", names)
        self.assertIn("SQL/fixture.txt", names)
        self.assertIn("srv/clients/fixture.txt", names)
        for name in names:
            self.assertFalse(name.startswith(("backups", "logs", "raw-postgres")))
            self.assertNotIn("node_modules", name)
        self.backup.verify()

    def test_dump_failure_stops_before_snapshot_and_retention(self):
        self.backup.failure = "dump-list"
        with self.assertRaises(module.BackupError):
            self.backup.backup("daily")
        self.assertNotIn("backup", self.actions())
        self.assertNotIn("forget", self.actions())

    def test_empty_dump_stops_before_snapshot(self):
        self.backup.dump_bytes = b""
        with self.assertRaises(module.BackupError):
            self.backup.backup("daily")
        self.assertNotIn("backup", self.actions())

    def test_cloud_failure_preserves_local_snapshot_and_skips_retention(self):
        self.backup.cfg["cloud_repository"] = "s3:example/fithub"
        self.backup.failure = "cloud"
        with self.assertRaises(module.BackupError):
            self.backup.backup("weekly")
        self.assertIn("backup", self.actions())
        self.assertNotIn("forget", self.actions())
        self.assertTrue(Path(self.backup.snapshot_row["paths"][0]).is_dir())

    def test_snapshot_failure_skips_retention(self):
        self.backup.failure = "backup"
        with self.assertRaises(module.BackupError):
            self.backup.backup("daily")
        self.assertNotIn("forget", self.actions())

    def test_restore_failure_skips_retention(self):
        with patch.object(self.backup, "restore_test", side_effect=module.BackupError("restore failed")):
            with self.assertRaises(module.BackupError):
                self.backup.backup("weekly", restore_test=True)
        self.assertNotIn("forget", self.actions())

    def test_checksum_detects_modified_dump_after_restoring_snapshot(self):
        self.backup.backup("daily")
        restored_dump = self.backup.vault / self.backup.current.relative_to("/") / "database/db-001.dump"
        restored_dump.write_bytes(b"corrupted")
        with self.assertRaisesRegex(module.BackupError, "Checksum errato"):
            self.backup.verify()

    def test_missing_required_directory_fails_before_dump(self):
        shutil.rmtree(self.root / "srv/clients")
        with self.assertRaises(module.BackupError):
            self.backup.backup("daily")
        self.assertFalse(self.backup.calls)

    def test_required_directory_cannot_be_a_symlink(self):
        shutil.rmtree(self.root / "library")
        (self.root / "library").symlink_to(self.root / "SQL")
        with self.assertRaisesRegex(module.BackupError, "symlink"):
            self.backup.sources()

    def test_retention_groups_by_host_and_tags_so_timestamped_bundles_expire(self):
        self.backup.retention(prune=True)
        for tool, args in self.backup.calls:
            if args[0] == "forget":
                self.assertEqual(args[args.index("--group-by") + 1], "host,tags")
                self.assertIn("--host", args)
                self.assertIn("--tag", args)

    def test_sync_retries_all_retained_snapshots(self):
        self.backup.backup("daily")
        self.backup.calls.clear()
        self.backup.cfg["cloud_repository"] = "s3:example/fithub"
        self.backup.sync()
        args = next(args for tool, args in self.backup.calls if args[0] == "copy")
        self.assertEqual(args[0], "copy")
        self.assertNotIn("latest", args)
        self.assertIn("--from-repo", args)

    def test_ssd_copy_contains_matching_dumps_and_restores_without_local_backup_files(self):
        self.backup.cfg["ssd_mount"] = "/Volumes/TestSSD"
        self.backup.backup("daily")
        local_dump_dir = self.backup.current
        external = self.base / "ssd" / local_dump_dir.relative_to("/")
        manifest = self.backup.validate(external)
        self.assertEqual([entry["name"] for entry in manifest["databases"]], ["fithub", "postgres"])
        shutil.rmtree(self.backup.vault)
        shutil.rmtree(local_dump_dir)
        shutil.rmtree(self.root / "FitHub")
        self.backup.calls.clear()
        self.backup.verify(ssd=True)
        restores = [(tool, args) for tool, args in self.backup.calls if args[0] == "restore"]
        self.assertEqual(len(restores), 1)
        self.assertEqual(restores[0][0], "ssd")

    def test_cloud_copy_contains_database_files_without_separate_plaintext_export(self):
        self.backup.cfg["cloud_repository"] = "s3:example/fithub"
        self.backup.backup("daily")
        external = self.base / "cloud" / self.backup.current.relative_to("/")
        self.backup.validate(external)
        self.backup.verify(cloud=True)
        self.assertTrue((external / "database/db-001.dump").is_file())

    def test_incomplete_external_snapshot_blocks_retention(self):
        self.backup.cfg["cloud_repository"] = "s3:example/fithub"
        self.backup.failure = "incomplete-copy"
        with self.assertRaisesRegex(module.BackupError, "Copie esterne non completate"):
            self.backup.backup("daily")
        self.assertNotIn("forget", self.actions())

    def test_ssd_is_attempted_even_when_cloud_fails_and_retention_is_skipped(self):
        self.backup.cfg.update(cloud_repository="s3:example/fithub", ssd_mount="/Volumes/TestSSD")
        self.backup.failure = "cloud"
        with self.assertRaises(module.BackupError):
            self.backup.backup("daily")
        self.assertTrue(any(tool == "ssd" and args[0] == "copy" for tool, args in self.backup.calls))
        self.assertNotIn("forget", self.actions())

    def test_missing_ssd_never_creates_a_directory_or_runs_restic(self):
        self.backup.cfg["ssd_mount"] = "/Volumes/TestSSD"
        with patch.object(Path, "is_mount", return_value=False), patch.object(self.backup, "run") as run:
            with self.assertRaisesRegex(module.BackupError, "SSD non montato"):
                module.Backup.restic(self.backup, "init", ssd=True)
        run.assert_not_called()

    def test_ssd_repository_cannot_escape_mount_or_use_volumes_itself(self):
        self.backup.cfg.update(ssd_mount="/Volumes/TestSSD", ssd_repository="../elsewhere")
        with patch.object(Path, "is_mount", return_value=True):
            with self.assertRaises(module.BackupError):
                self.backup.ssd_repository()
        self.backup.cfg.update(ssd_mount="/Volumes", ssd_repository="restic")
        with self.assertRaises(module.BackupError):
            self.backup.ssd_repository()

    def test_existing_local_configuration_without_ssd_fields_still_works(self):
        for name in ("ssd_mount", "ssd_repository", "ssd_password_file"):
            self.backup.cfg.pop(name)
        self.backup.backup("daily")
        self.backup.validate(self.backup.current)
        self.assertFalse(any(tool in ("ssd", "cloud") for tool, _ in self.backup.calls))
        self.assertFalse((self.backup.store / "daily").exists())

    def test_explicit_ssd_sync_does_not_attempt_configured_cloud(self):
        self.backup.backup("daily")
        self.backup.calls.clear()
        self.backup.cfg.update(cloud_repository="s3:example/fithub", ssd_mount="/Volumes/TestSSD")
        self.backup.failure = "cloud"
        self.backup.sync(ssd=True)
        self.assertFalse(any(tool == "cloud" for tool, _ in self.backup.calls))
        self.assertIn("ssd", self.backup.external_rows)

    def test_weekly_external_copy_recovers_archive_and_database_together(self):
        self.backup.cfg["ssd_mount"] = "/Volumes/TestSSD"
        self.backup.backup("weekly")
        target = Path(self.backup.snapshot_row["paths"][0])
        copied = self.base / "ssd" / target.relative_to("/")
        self.backup.validate(copied)
        self.assertTrue((copied / "application.tar.gz").is_file())
        self.assertTrue((copied / "database/db-001.dump").is_file())
        shutil.rmtree(self.backup.vault)
        shutil.rmtree(target)
        self.backup.verify(ssd=True)

    def test_process_exit_three_is_failure_not_partial_success(self):
        with self.assertRaisesRegex(module.BackupError, "codice 3"):
            self.backup.run([sys.executable, "-c", "raise SystemExit(3)"])

    def test_mutual_exclusion_and_automatic_lock_release(self):
        self.backup.store.mkdir(exist_ok=True)
        with (self.backup.store / ".backup.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(module.BackupError, "già in esecuzione"):
                with self.backup.locked("daily"):
                    self.fail("must not enter")
        with self.backup.locked("daily"):
            pass
        self.assertTrue(json.loads((self.backup.store / "status-daily.json").read_text())["ok"])

    def test_failed_run_records_failure_status(self):
        with self.assertRaises(module.BackupError):
            with self.backup.locked("daily"):
                raise module.BackupError("intentional fixture error")
        self.assertFalse(json.loads((self.backup.store / "status-daily.json").read_text())["ok"])

    def test_restore_test_uses_isolated_image_and_cleans_up_on_failure(self):
        self.backup.backup("daily")
        calls = []
        def run(args, **kwargs):
            args = list(map(str, args))
            calls.append(args)
            if "pg_restore" in args:
                raise module.BackupError("restore failed")
            return ""
        with patch.object(self.backup, "run", side_effect=run), patch.object(
                module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaises(module.BackupError):
                self.backup.restore_test(self.backup.current, self.backup.validate(self.backup.current))
        start = calls[0]
        self.assertIn("--pull=never", start)
        self.assertEqual(start[start.index("--network") + 1], "none")
        self.assertEqual(start[-1], "sha256:immutable-image")
        self.assertNotIn("production-id", str(calls))
        self.assertEqual(calls[-1][:4], ["docker", "rm", "--force", "--volumes"])
        self.assertEqual(calls[-1][-1], start[start.index("--name") + 1])

    def test_private_password_and_sanitized_restic_environment(self):
        password = self.base / "restic-password"
        password.write_text("not-a-real-password")
        password.chmod(0o600)
        self.backup.cfg["password_file"] = str(password)
        with patch.dict(os.environ, {"RESTIC_PASSWORD": "wrong", "RESTIC_REPOSITORY": "wrong"}), patch.object(
                self.backup, "run", return_value="{}") as run:
            module.Backup.restic(self.backup, "check")
        env = run.call_args.kwargs["env"]
        self.assertNotIn("RESTIC_PASSWORD", env)
        self.assertNotIn("RESTIC_REPOSITORY", env)
        password.chmod(0o644)
        with self.assertRaises(module.BackupError):
            module.Backup.restic(self.backup, "check")

    def test_restore_test_restores_globals_and_each_database_with_create(self):
        self.backup.backup("daily")
        calls = []
        def run(args, **kwargs):
            calls.append(list(map(str, args)))
            return "2\n"
        with patch.object(self.backup, "run", side_effect=run), patch.object(
                module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as ready:
            self.backup.restore_test(self.backup.current, self.backup.validate(self.backup.current))
        self.assertIn("127.0.0.1", ready.call_args.args[0])
        restores = [a for a in calls if "pg_restore" in a]
        self.assertEqual(len(restores), 2)
        for args in restores:
            self.assertIn("--create", args)
            self.assertIn("--exit-on-error", args)
            self.assertEqual(args[args.index("--dbname") + 1], "template1")
        self.assertIn("ON_ERROR_STOP=1", calls[1])

    def test_postgres_discovery_uses_existing_compose_env_without_overriding_project_directory(self):
        self.backup.pg = None
        (self.root / "configs/fithub-postgres17.yml").write_text("services: {}")
        info = [{"Id": "detected-id", "Image": "sha256:detected-image", "State": {"Running": True},
                 "Config": {"Env": ["POSTGRES_USER=actual-user", "PGDATA=/var/lib/postgresql/data"]},
                 "Mounts": []}]
        with patch.object(self.backup, "run", side_effect=["detected-id\n", json.dumps(info)]) as run:
            actual = self.backup.postgres()
        self.assertEqual(actual["user"], "actual-user")
        self.assertEqual(actual["image"], "sha256:detected-image")
        args = run.call_args_list[0].args[0]
        self.assertNotIn("--project-directory", args)
        self.assertIn(self.root / "configs/.env", args)

    def test_monthly_checks_mounted_destination_and_accepts_first_time_machine_backup(self):
        self.backup.cfg.update(time_machine_destination_id="expected-id", time_machine_mount="/Volumes/Backup SSD")
        text = "====================================================\nName          : Backup SSD\nKind          : Local\nMount Point   : /Volumes/Backup SSD\nID            : expected-id\n"
        with patch.object(module.sys, "platform", "darwin"), patch.object(Path, "is_mount", return_value=True), patch.object(
                self.backup, "run", side_effect=[text, None, "new-completed-backup\n"]) as run, patch.object(
                self.backup, "backup") as backup, patch.object(module.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 1, stdout=b"")):
            self.backup.monthly()
        backup.assert_called_once_with("weekly", restore_test=True)
        args = run.call_args_list[1].args[0]
        self.assertIn("--block", args)
        self.assertEqual(args[-1], "expected-id")

    def test_failed_docker_run_does_not_remove_existing_container(self):
        self.backup.backup("daily")
        with patch.object(self.backup, "run", side_effect=module.BackupError("run failed")) as run:
            with self.assertRaises(module.BackupError):
                self.backup.restore_test(self.backup.current, self.backup.validate(self.backup.current))
        self.assertEqual(run.call_count, 1)

    def test_runtime_rejects_old_branches_old_patches_and_prereleases(self):
        versions = [(3, 9, 25, "final"), (3, 10, 22, "final"), (3, 13, 17, "final"),
                    (3, 14, 8, "final"), (3, 14, 10, "candidate"), (3, 15, 1, "final")]
        for major, minor, micro, level in versions:
            with self.subTest(version=(major, minor, micro, level)), patch.object(module.sys, "version_info",
                    SimpleNamespace(major=major, minor=minor, micro=micro, releaselevel=level)):
                with self.assertRaises(module.BackupError):
                    module.check_runtime()

    def test_runtime_accepts_current_baseline_and_later_stable_patch(self):
        for micro in (9, 10):
            with patch.object(module.sys, "version_info", SimpleNamespace(
                    major=3, minor=14, micro=micro, releaselevel="final")):
                module.check_runtime()

    def test_runtime_rejection_happens_before_configuration_and_backups(self):
        with patch.object(sys, "argv", ["backup.py", "daily"]), patch.object(
                module, "check_runtime", side_effect=module.BackupError("runtime rejected")), patch.object(
                module, "Backup") as backup, patch("builtins.print"):
            self.assertEqual(module.main(), 1)
        backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
