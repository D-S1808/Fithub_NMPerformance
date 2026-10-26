#!/usr/bin/env python3.14
"""FitHub update checker: local inventory and public catalogs, without accounts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import platform
import plistlib
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"v?(\d+)(?:\.(\d+))(?:\.(\d+))?(?:\.(\d+))?(?:_(\d+))?\Z")
CONTAINER_TEMPLATE = '{"name":{{json .Name}},"image":{{json .Config.Image}},"id":{{json .Image}},"state":{{json .State.Status}}}'
# Optional fields differ across CLI/Engine versions; select them in Python.
IMAGE_TEMPLATE = "{{json .}}"


class CheckError(Exception):
    """Only safe, deliberately constructed messages may reach reports."""


def command_failure(args, code, stderr):
    """Classify known failures without copying arguments, URLs or stderr."""
    program = Path(args[0]).name
    message = stderr.decode("utf-8", errors="replace").lower()
    reason = "comando fallito"
    if program == "git":
        cases = [(('detected dubious ownership', 'unsafe repository'), "proprietario del repository non coerente con l'utente di esecuzione"),
                 (('host key verification failed',), "verifica della chiave host SSH fallita"),
                 (('permission denied (publickey', 'could not read username', 'could not read password', 'authentication failed', 'terminal prompts disabled'), "autenticazione remota non disponibile senza interazione"),
                 (('repository not found',), "repository remoto non accessibile o credenziali non autorizzate"),
                 (("couldn't find remote ref", 'remote ref does not exist'), "branch remoto non trovato"),
                 (('no upstream configured', 'no upstream branch'), "upstream non configurato"),
                 (('not a git repository',), "directory non riconosciuta come repository Git"),
                 (('could not resolve host', 'could not resolve hostname'), "DNS del server remoto non risolto"),
                 (('certificate',), "verifica TLS del server remoto fallita"),
                 (('permission denied', 'operation not permitted'), "accesso a repository o file di autenticazione negato"),
                 (('failed to connect', 'connection refused', 'connection timed out'), "connessione al server remoto non riuscita")]
    elif program == "docker":
        cases = [(('error getting credentials', 'credentials store', 'keychain'), "credenziali registry/portachiavi non disponibili in questa sessione"),
                 (('toomanyrequests', 'too many requests', '429'), "limite di richieste del registry raggiunto"),
                 (('unauthorized', 'authentication required', 'insufficient_scope', 'pull access denied', 'authorization failed', 'access denied', '403 forbidden'), "accesso al registry negato o immagine non pubblicata/accessibile"),
                 (('manifest unknown', 'no such manifest', 'not found'), "immagine, tag o digest non trovato"),
                 (('cannot connect to the docker daemon', 'is the docker daemon running'), "daemon Docker non raggiungibile"),
                 (('no such host', 'could not resolve', 'server misbehaving'), "DNS del registry non risolto"),
                 (('certificate', 'tls handshake'), "verifica/negoziazione TLS con il registry fallita"),
                 (('timeout', 'deadline exceeded'), "timeout verso il registry"),
                 (('500 internal server', '502 bad gateway', '503 service unavailable'), "errore temporaneo del registry/proxy"),
                 (('unknown flag', 'is not a docker command', 'is not a docker-command'), "CLI/Buildx non compatibile con il controllo richiesto")]
    else:
        cases = []
    for patterns, description in cases:
        if any(p in message for p in patterns):
            reason = description
            break
    return CheckError(f"{program}: {reason} (codice {code})")


def operation(label, action):
    try:
        return action()
    except CheckError as error:
        raise type(error)(label + ": " + str(error)) from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CheckError(label + ": metadati assenti o non validi") from None


def runtime_check():
    v = sys.version_info
    if (sys.implementation.name != "cpython" or v[:2] != (3, 14) or v[:3] < (3, 14, 9)
            or v.releaselevel != "final"):
        raise CheckError("Richiesto CPython stabile 3.14.9+ del ramo 3.14, come per i backup")


def version_key(value):
    match = VERSION.fullmatch(str(value))
    return tuple(int(x or 0) for x in match.groups()) if match else None


def change_kind(installed, available):
    a, b = version_key(installed), version_key(available)
    if a and b:
        if b <= a:
            return "OK"
        return "MAJOR" if a[0] != b[0] and b[0] < 2000 else "UPDATE"
    return "OK" if installed == available else "UPDATE"


def image_ref(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", value)
            or "://" in value or value.count("@") > 1):
        raise CheckError("Riferimento immagine non valido")
    base, _, digest = value.partition("@")
    if digest and not DIGEST.fullmatch(digest):
        raise CheckError("Digest immagine non valido")
    last = base.rsplit("/", 1)[-1]
    repository, tag = base.rsplit(":", 1) if ":" in last else (base, "latest")
    return repository, tag, digest


def configured(script_dir=SCRIPT_DIR):
    path = script_dir / "updatecheck.json"
    try:
        cfg = json.loads((path if path.exists() else script_dir / "updatecheck.example.json").read_text())
        if not isinstance(cfg, dict):
            raise ValueError()
        if not isinstance(cfg["docker"], dict) or not isinstance(cfg["git"], dict):
            raise ValueError()
        for key in ["docker_desktop", "macos", "homebrew", "down_on_updates"]:
            if type(cfg[key]) is not bool:
                raise ValueError()
        apps = cfg.setdefault("applications", dict(enabled=True, directories=["/Applications", "~/Applications",
                              "/System/Applications", "/System/Library/CoreServices/Applications"],
                              app_store_country="ch", cask_overrides={}))
        if (not isinstance(apps, dict) or type(apps["enabled"]) is not bool
                or not isinstance(apps["directories"], list) or not apps["directories"]
                or not all(isinstance(p, str) and (p.startswith("/") or p.startswith("~/")) for p in apps["directories"])
                or not isinstance(apps["app_store_country"], str)
                or not re.fullmatch(r"[A-Za-z]{2}", apps["app_store_country"])
                or not isinstance(apps["cask_overrides"], dict)
                or not all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[a-z0-9][a-z0-9@+_.-]*", v)
                           for k, v in apps["cask_overrides"].items())):
            raise ValueError()
        for key in ["docker", "git"]:
            if type(cfg[key]["enabled"]) is not bool:
                raise ValueError()
        excludes = cfg["docker"]["exclude"]
        if not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes):
            raise ValueError()
        local_images = cfg["docker"].setdefault("local_images", ["fithub-backend:local", "fithub-frontend:local"])
        if not isinstance(local_images, list):
            raise ValueError()
        for value in local_images:
            image_ref(value)
        if not isinstance(cfg["git"]["path"], str) or not cfg["git"]["path"]:
            raise ValueError()
        schedule = cfg["schedule"]
        for key, maximum in [("weekday", 7), ("hour", 23), ("minute", 59)]:
            if type(schedule[key]) is not int or not 0 <= schedule[key] <= maximum:
                raise ValueError()
        return cfg
    except (OSError, ValueError, KeyError, TypeError):
        raise CheckError("Configurazione updatecheck.json non leggibile o non valida") from None


def command(args, timeout=30, env=None, accepted=(0,)):
    child_env = dict(os.environ, LC_ALL="C", GIT_TERMINAL_PROMPT="0",
                     HOME=pwd.getpwuid(os.getuid()).pw_dir, SSH_ASKPASS_REQUIRE="never",
                     HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ANALYTICS="1",
                     HOMEBREW_NO_ENV_HINTS="1")
    child_env.update(env or {})
    try:
        process = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                   env=child_env, start_new_session=True)
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise CheckError(f"Timeout del comando {Path(args[0]).name}") from None
    except OSError:
        raise CheckError(f"Impossibile avviare {Path(args[0]).name}") from None
    if process.returncode not in accepted:
        # stderr may contain credentials, private repository URLs or registry tokens.
        raise command_failure(args, process.returncode, err)
    return out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


def command_json(args, **kwargs):
    try:
        return json.loads(command(args, **kwargs)[0])
    except (ValueError, TypeError):
        raise CheckError("Risposta JSON del comando non valida") from None


def get_bytes(url, max_bytes=4 * 1024 * 1024):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "FitHub-updatecheck/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise CheckError("Risposta HTTP inattesa dal catalogo aggiornamenti")
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise CheckError("Catalogo aggiornamenti troppo grande")
        return data
    except (OSError, urllib.error.URLError, ValueError):
        raise CheckError("Catalogo aggiornamenti non raggiungibile") from None


def get_json(url, max_bytes=4 * 1024 * 1024):
    try:
        return json.loads(get_bytes(url, max_bytes=max_bytes))
    except ValueError:
        raise CheckError("Catalogo aggiornamenti JSON non valido") from None


def tool(name):
    candidates = {"docker": ["/Applications/Docker.app/Contents/Resources/bin/docker", "/usr/local/bin/docker"],
                  "brew": ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]}
    for path in candidates.get(name, []):
        if os.access(path, os.X_OK):
            return path
    return shutil.which(name)


def row(area, name, status, installed="", available="", detail=""):
    return dict(area=area, name=name, status=status, installed=str(installed),
                available=str(available), detail=detail)


def image_repository(reference):
    repository = image_ref(reference)[0]
    for host in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        repository = repository.removeprefix(host)
    return repository.removeprefix("library/")


def image_version(config, reference):
    labels = config.get("Labels") or {}
    value = labels.get("org.opencontainers.image.version", "")
    if value and version_key(value):
        return value.lstrip("v")
    values = dict(e.split("=", 1) for e in (config.get("Env") or []) if "=" in e)
    key = {"postgres": "PG_VERSION", "redis": "REDIS_VERSION", "mysql": "MYSQL_VERSION",
           "mariadb": "MARIADB_VERSION", "node": "NODE_VERSION"}.get(image_repository(reference))
    if key:
        value = values.get(key, "").split("-", 1)[0]
        if version_key(value):
            return value
    return ""


def service_versions(repository):
    """Public software release metadata; no Docker registry or credentials."""
    if repository in ("postgres", "redis", "mysql"):
        url = f"https://raw.githubusercontent.com/docker-library/{repository}/master/versions.json"
        data = get_json(url)
        if not isinstance(data, dict):
            raise CheckError("Catalogo pubblico del servizio non valido")
        values = [entry.get("version", "") for entry in data.values() if isinstance(entry, dict)]
    elif repository == "node":
        data = get_json("https://nodejs.org/dist/index.json")
        if not isinstance(data, list):
            raise CheckError("Catalogo pubblico Node non valido")
        values = [entry.get("version", "") for entry in data if isinstance(entry, dict)]
    elif repository == "louislam/uptime-kuma":
        data = get_json("https://api.github.com/repos/louislam/uptime-kuma/releases?per_page=30")
        if not isinstance(data, list):
            raise CheckError("Catalogo pubblico Kuma non valido")
        values = [entry.get("tag_name", "") for entry in data if isinstance(entry, dict)
                  and entry.get("draft") is False and entry.get("prerelease") is False]
    else:
        return []
    versions = [value.lstrip("v") for value in values if isinstance(value, str) and version_key(value)]
    if not versions:
        raise CheckError("Catalogo pubblico senza versioni stabili riconoscibili")
    return versions


def docker_rows(cfg):
    docker = tool("docker")
    if not docker:
        return [row("Docker", "Servizi", "SKIP", detail="CLI Docker non installata")]
    rows, catalogs = [], {}
    ids = command([docker, "container", "ls", "--all", "--format", "{{.ID}}"])[0].split()
    if not ids:
        return [row("Docker", "Servizi", "SKIP", detail="Nessun container installato")]
    for cid in ids:
        if not re.fullmatch(r"[0-9a-f]{12,64}", cid):
            raise CheckError("Identificativo container non valido")
        name, current_ref, installed = cid, "", ""
        try:
            container = operation("Docker: lettura container locale", lambda: command_json(
                [docker, "container", "inspect", "--format", CONTAINER_TEMPLATE, cid]))
            name = container["name"].lstrip("/")
            current_ref = container["image"]
            image_ref(current_ref)
            if name in cfg["exclude"]:
                rows.append(row("Docker", name, "SKIP", current_ref, detail="Escluso dalla configurazione"))
                continue
            if current_ref in cfg.get("local_images", ["fithub-backend:local", "fithub-frontend:local"]):
                rows.append(row("Docker", name, "SKIP", current_ref,
                                detail="Build locale FitHub: censita, aggiornamenti da verificare sui sorgenti"))
                continue
            if not DIGEST.fullmatch(container["id"]):
                raise CheckError("Identificativo immagine locale non valido")
            image = operation("Docker: lettura immagine locale", lambda: command_json(
                [docker, "image", "inspect", "--format", IMAGE_TEMPLATE, container["id"]]))
            installed = image_version(image.get("Config") or {}, current_ref)
            repository = image_repository(current_ref)
            if repository == "louislam/uptime-kuma" and not installed and container.get("state", "running") == "running":
                try:
                    package = command_json([docker, "exec", cid, "/bin/cat", "/app/package.json"])
                    value = package.get("version", "")
                    if package.get("name") == "uptime-kuma" and version_key(value):
                        installed = value.lstrip("v")
                except (CheckError, AttributeError, TypeError):
                    pass
            detail = "Immagine " + current_ref + "; stato " + container.get("state", "non disponibile")
            if repository not in ("postgres", "redis", "mysql", "node", "louislam/uptime-kuma") or not installed:
                rows.append(row("Docker", name, "SKIP", installed or current_ref,
                                detail=detail + "; versione o fonte pubblica non riconosciuta: aggiornamenti non verificati"))
                continue
            # Reuse each catalog, including failures, within this scan.
            if repository not in catalogs:
                try:
                    catalogs[repository] = service_versions(repository)
                except CheckError as error:
                    catalogs[repository] = error
            versions = catalogs[repository]
            if isinstance(versions, CheckError):
                raise versions
            width = 2 if repository in ("redis", "mysql") else 1
            channel = version_key(installed)[:width]
            matches = [v for v in versions if version_key(v)[:width] == channel]
            available = max(matches or versions, key=version_key)
            state = change_kind(installed, available)
            if not matches:
                # A newer stable branch is useful to report, but is not a patch of this branch.
                state = "REVIEW" if version_key(available) > version_key(installed) else "SKIP"
                detail += "; ramo installato assente dal catalogo, verificare il passaggio alla versione indicata"
            else:
                detail += "; confronto versione software nel ramo " + ".".join(map(str, channel))
            detail += "; fonte pubblica, ricostruzioni Docker non verificate"
            rows.append(row("Docker", name, state, installed, available, detail))
        except (CheckError, KeyError, TypeError, ValueError, AttributeError) as error:
            detail = str(error) if isinstance(error, CheckError) else "Metadati locali/catalogo non validi"
            rows.append(row("Docker", name, "ERROR", installed or current_ref, detail=detail))
    return rows


def desktop_rows():
    app = Path("/Applications/Docker.app/Contents/Info.plist")
    if not app.is_file():
        return [row("Host", "Docker Desktop", "SKIP", detail="Applicazione non presente in /Applications/Docker.app")]
    try:
        installed = plistlib.loads(app.read_bytes())["CFBundleShortVersionString"]
        arch = "arm64" if platform.machine() == "arm64" else "amd64"
        source = "feed stabile Docker"
        try:
            feed = ET.fromstring(get_bytes(f"https://desktop.docker.com/mac/main/{arch}/appcast.xml"))
            versions = []
            for element in feed.iter():
                versions.extend(v for k, v in element.attrib.items() if k.endswith("shortVersionString") and version_key(v))
                if element.tag.endswith("shortVersionString") and version_key(element.text):
                    versions.append(element.text)
            if not versions:
                raise CheckError("Feed Docker senza versione stabile")
            latest = max(versions, key=version_key)
        except (CheckError, ET.ParseError):
            latest = get_json("https://formulae.brew.sh/api/cask/docker-desktop.json")["version"].split(",")[0]
            source = "catalogo Homebrew docker-desktop (ripiego sul feed Docker non disponibile)"
        if not version_key(installed) or not version_key(latest):
            raise CheckError("Versione Docker Desktop non valida")
        return [row("Host", "Docker Desktop", change_kind(installed, latest), installed, latest, source)]
    except (OSError, KeyError, TypeError, AttributeError, plistlib.InvalidFileException):
        raise CheckError("Impossibile leggere la versione di Docker Desktop") from None


def macos_rows():
    current = command(["/usr/bin/sw_vers", "-productVersion"])[0].strip()
    out, err = command(["/usr/sbin/softwareupdate", "--list"], timeout=240)
    output = out + "\n" + err
    labels = re.findall(r"^[ \t]*[*-][ \t]*Label:[ \t]*(.+)$", output, re.M)
    if labels:
        items = []
        for label in labels:
            state = "UPDATE"
            match = re.search(r"macOS[^\n]*?-(\d+\.\d+(?:\.\d+)?)(?:-|$)", label)
            if match and change_kind(current, match[1]) == "MAJOR":
                state = "MAJOR"
            items.append(row("Host", "macOS / Apple", state, current, label.strip(),
                             "Proposto da softwareupdate --list; nessun download/installazione"))
        return items
    if "No new software available" in output or "No updates are available" in output:
        return [row("Host", "macOS / Apple", "OK", current, detail="Nessun aggiornamento proposto da Apple")]
    raise CheckError("Output di softwareupdate non riconosciuto; controllo non concluso")


def brew_rows(applications=None):
    brew = tool("brew")
    if not brew:
        return [row("Homebrew", "Pacchetti", "SKIP", detail="Homebrew non installato")]
    inventory = command_json([brew, "info", "--json=v2", "--installed"], timeout=120)
    if (not isinstance(inventory, dict) or not isinstance(inventory.get("formulae"), list)
            or not isinstance(inventory.get("casks"), list)):
        raise CheckError("Inventario Homebrew non valido")
    tasks = [("formula", x) for x in inventory["formulae"]] + [("cask", x) for x in inventory["casks"]]
    verified_apps = {Path(x["app_path"]).name.casefold() for x in applications or []
                     if x.get("app_path") and x["status"] in ("OK", "UPDATE", "MAJOR", "REVIEW")}

    def check(task):
        kind, package = task
        if not isinstance(package, dict):
            return row("Homebrew", "Pacchetto", "ERROR", detail="Metadati pacchetto non validi")
        name = package.get("name") if kind == "formula" else package.get("token")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9@+_.-]+", name):
            return row("Homebrew", "Pacchetto", "ERROR", detail="Nome pacchetto non valido")
        if package.get("tap") != ("homebrew/core" if kind == "formula" else "homebrew/cask"):
            return row("Homebrew", name, "SKIP", detail="Tap terzo: nessun catalogo ufficiale configurato")
        if kind == "cask":
            names = {Path(n).name.casefold() for a in package.get("artifacts") or [] if isinstance(a, dict)
                     for n in a.get("app") or [] if isinstance(n, str)}
            if names and names.issubset(verified_apps):
                return row("Homebrew", name, "SKIP", detail="Versione del binario gia verificata nel gruppo Applicazioni; ricevuta Homebrew non usata")
        try:
            catalog = get_json("https://formulae.brew.sh/api/" + kind + "/" + urllib.parse.quote(name, safe="@") + ".json")
            if kind == "formula":
                versions = [x["version"] for x in package["installed"]]
                latest = catalog["versions"]["stable"]
                revision = catalog.get("revision", 0)
                if revision:
                    latest += "_" + str(revision)
            else:
                value = package.get("installed")
                versions = value if isinstance(value, list) else [value]
                latest = catalog["version"]
            if not versions or not all(isinstance(v, str) and v for v in versions) or not isinstance(latest, str) or not latest:
                raise CheckError("Versioni del pacchetto non valide")
            installed = max(versions, key=lambda v: version_key(v) or (-1,))
            detail = "Versione registrata da Homebrew; catalogo ufficiale aggiornato via API"
            if package.get("auto_updates"):
                detail += "; app con auto-update: il registro Homebrew può differire dal binario"
            if package.get("pinned"):
                detail += "; pacchetto fissato (pinned)"
            if installed.startswith("HEAD") or latest == "latest":
                return row("Homebrew", name, "SKIP", installed, latest, "Versione HEAD/latest non confrontabile")
            return row("Homebrew", name, change_kind(installed, latest), installed, latest, detail)
        except (CheckError, KeyError, TypeError, AttributeError) as error:
            return row("Homebrew", name, "ERROR", detail=str(error) if isinstance(error, CheckError) else "Catalogo pacchetto non valido")
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(check, tasks)) or [row("Homebrew", "Pacchetti", "OK", detail="Nessun pacchetto installato")]


def numeric_version(value):
    """Conservative numeric ordering, including integer Sparkle build numbers."""
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+(?:\.\d+){0,15}", value):
        return None
    parts = tuple(int(x) for x in value.lstrip("v").split("."))
    return parts + (0,) * (16 - len(parts))


def app_version_status(installed, available):
    old, new = numeric_version(installed), numeric_version(available)
    if old is None or new is None:
        return "OK" if installed and installed == available else "SKIP"
    if new <= old:
        return "OK"
    return change_kind(installed, available)


def application_inventory(directories):
    apps, errors, seen = [], [], set()
    def scan_error(error):
        errors.append(row("Applicazioni", "Inventario", "ERROR", detail="Directory app non leggibile: " + str(error.filename)))
    def add(path):
        path = path.resolve()
        if path in seen:
            return
        seen.add(path)
        try:
            info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
            if not isinstance(info, dict):
                raise ValueError()
            def value(key):
                data = info.get(key, "")
                return data if isinstance(data, str) else ""
            apps.append(dict(path=path, name=value("CFBundleDisplayName") or value("CFBundleName") or path.stem,
                             bundle_id=value("CFBundleIdentifier"), version=value("CFBundleShortVersionString") or value("CFBundleVersion"),
                             build=value("CFBundleVersion"), feed=value("SUFeedURL"),
                             store=(path / "Contents/_MASReceipt/receipt").is_file()))
        except (OSError, ValueError, plistlib.InvalidFileException):
            item = row("Applicazioni", path.stem, "ERROR", detail="Info.plist assente o non leggibile")
            item["app_path"] = str(path)
            errors.append(item)
    for directory in directories:
        root = Path(pwd.getpwuid(os.getuid()).pw_dir) / directory[2:] if directory.startswith("~/") else Path(directory)
        if not root.exists():
            continue
        if root.suffix.lower() == ".app":
            add(root)
            continue
        for parent, names, _ in os.walk(root, onerror=scan_error, followlinks=False):
            for name in sorted(names):
                if name.lower().endswith(".app"):
                    add(Path(parent) / name)
            # Helpers, embedded runtimes and frameworks inside an app are not separate installations.
            names[:] = sorted(n for n in names if not n.startswith(".") and not n.lower().endswith(".app"))
    return sorted(apps, key=lambda a: (a["name"].casefold(), str(a["path"]))), errors


def cask_app_index(catalog):
    if not isinstance(catalog, list) or not all(isinstance(c, dict) for c in catalog):
        raise CheckError("Catalogo applicazioni Homebrew non valido")
    by_id, by_token = {}, {}
    for cask in catalog:
        token = cask.get("token", "")
        if not isinstance(token, str) or not re.fullmatch(r"[a-z0-9][a-z0-9@+_.-]*", token):
            continue
        by_token[token] = cask
        ids, names = set(), set()
        for artifact in cask.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            for name in artifact.get("app") or []:
                if isinstance(name, str):
                    names.add(Path(name).name.casefold())
            for key in ("uninstall", "zap"):
                for action in artifact.get(key) or []:
                    if not isinstance(action, dict):
                        continue
                    quits = action.get("quit", [])
                    quits = [quits] if isinstance(quits, str) else quits
                    if isinstance(quits, list):
                        ids.update(q.casefold() for q in quits if isinstance(q, str) and re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", q))
                    trash = action.get("trash", [])
                    trash = [trash] if isinstance(trash, str) else trash
                    if isinstance(trash, list):
                        for entry in trash:
                            if not isinstance(entry, str):
                                continue
                            match = re.fullmatch(r"~/Library/Preferences/([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+)\.plist", entry)
                            if match:
                                ids.add(match[1].casefold())
        for identifier in ids:
            by_id.setdefault(identifier, []).append((cask, names))
    return by_id, by_token


def cask_application(app, cask, os_version):
    installed = app["version"]
    available = cask.get("bundle_short_version") or cask.get("version", "").split(",")[0]
    if not isinstance(available, str) or not available or available == "latest":
        return "SKIP", "", "Versione del catalogo non confrontabile"
    source = "Catalogo Homebrew cask: " + cask["token"] + "; versione letta dal binario .app"
    if cask.get("disabled"):
        return "REVIEW", available, source + "; distribuzione disabilitata nel catalogo"
    requirements = (cask.get("depends_on") or {}).get("macos") or {}
    for operator, values in requirements.items():
        if not isinstance(values, list):
            return "SKIP", available, source + "; requisiti macOS non interpretabili"
        current = numeric_version(os_version)
        required = [numeric_version(v) for v in values]
        if current is None or any(v is None for v in required):
            return "SKIP", available, source + "; requisiti macOS non interpretabili"
        compare = {">=": lambda v: current >= v, ">": lambda v: current > v,
                   "<=": lambda v: current <= v, "<": lambda v: current < v,
                   "==": lambda v: current == v}
        if operator not in compare:
            return "SKIP", available, source + "; requisiti macOS non interpretabili"
        if not any(compare[operator](v) for v in required):
            return "REVIEW", available, source + "; versione di catalogo incompatibile con questo macOS"
    # Some casks publish the full build (e.g. 16.108.26092039), while the app's
    # marketing version is just 16.108. Compare like with like when identifiable.
    if not cask.get("bundle_short_version") and app["build"] and available == app["build"]:
        status = "OK"
        source += "; catalogo corrispondente alla build installata"
    elif (not cask.get("bundle_short_version") and installed and app["build"].startswith(installed + ".")
          and available.startswith(installed + ".")):
        status = app_version_status(app["build"], available)
        source += "; confronto della build completa"
    else:
        status = app_version_status(installed, available)
    if status == "OK" and numeric_version(installed) == numeric_version(available):
        build = cask.get("bundle_version")
        if build and app["build"]:
            status = app_version_status(app["build"], build)
            if status == "MAJOR":
                status = "UPDATE"  # Internal build numbers are not marketing major versions.
    if status == "SKIP":
        source += "; versioni/build non confrontabili con certezza"
    return status, available, source


SPARKLE_NS = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def sparkle_application(app, os_version, architecture):
    feed = urllib.parse.urlsplit(app["feed"])
    if feed.scheme != "https" or not feed.netloc or feed.username or feed.password or "$" in app["feed"]:
        return "SKIP", "", "Feed Sparkle non HTTPS o dinamico: controllo manuale"
    try:
        document = ET.fromstring(get_bytes(app["feed"]))
    except ET.ParseError:
        raise CheckError("Feed Sparkle XML non valido") from None
    current_build = numeric_version(app["build"])
    current_os = numeric_version(os_version)
    if current_build is None or current_os is None:
        return "SKIP", "", "Build installata o versione macOS non confrontabile con Sparkle"
    candidates = []
    for item in document.findall(".//item"):
        def field(key, enclosure=None):
            return (item.findtext(SPARKLE_NS + key) or (enclosure.attrib.get(SPARKLE_NS + key) if enclosure is not None else "") or "").strip()
        if field("channel"):
            continue
        restricted = False
        for key, predicate in [("minimumSystemVersion", lambda v: current_os >= v),
                               ("maximumSystemVersion", lambda v: current_os <= v),
                               ("minimumUpdateVersion", lambda v: current_build >= v)]:
            value = field(key)
            if value and (numeric_version(value) is None or not predicate(numeric_version(value))):
                restricted = True
        hardware = field("hardwareRequirements")
        if hardware and hardware != architecture:
            restricted = True
        if restricted:
            continue
        for enclosure in item.findall("enclosure"):
            if enclosure.attrib.get(SPARKLE_NS + "os", "macos") != "macos" or enclosure.attrib.get(SPARKLE_NS + "deltaFrom"):
                continue
            build = field("version", enclosure)
            version = field("shortVersionString", enclosure) or build
            number = numeric_version(build)
            if number is not None:
                candidates.append((number, version, build))
    if not candidates:
        return "SKIP", "", "Sparkle: nessuna release numerica del canale predefinito verificabile per questo Mac"
    number, version, build = max(candidates, key=lambda c: c[0])
    status = "OK" if number <= current_build else "MAJOR" if change_kind(app["version"], version) == "MAJOR" else "UPDATE"
    return status, version, "Feed Sparkle dell'app, canale predefinito; build " + app["build"] + " → " + build


def store_application(app, country, os_version):
    query = urllib.parse.urlencode(dict(bundleId=app["bundle_id"], entity="macSoftware", country=country.lower()))
    data = get_json("https://itunes.apple.com/lookup?" + query)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise CheckError("Risposta Mac App Store non valida")
    matches = [r for r in data["results"] if isinstance(r, dict) and r.get("bundleId") == app["bundle_id"]
               and r.get("kind") == "mac-software"]
    if len(matches) != 1:
        return "SKIP", "", "Mac App Store: app non identificata nello store " + country.upper()
    entry = matches[0]
    available = entry.get("version")
    if not isinstance(available, str) or not available:
        raise CheckError("Versione Mac App Store non valida")
    minimum = entry.get("minimumOsVersion")
    if minimum and (numeric_version(minimum) is None or numeric_version(os_version) is None):
        return "SKIP", available, "Mac App Store: requisiti macOS non interpretabili"
    if minimum and numeric_version(os_version) < numeric_version(minimum):
        return "REVIEW", available, "Mac App Store: richiede macOS " + minimum + " o successivo"
    status = app_version_status(app["version"], available)
    return status, available, "Mac App Store " + country.upper() + "; catalogo pubblico, disponibilita per account non verificata"


def application_rows(cfg):
    apps, result = application_inventory(cfg["directories"])
    if not apps:
        return result or [row("Applicazioni", "Inventario", "ERROR", detail="Nessuna app trovata nelle directory configurate")]
    os_version = command(["/usr/bin/sw_vers", "-productVersion"])[0].strip()
    by_id = by_token = None
    catalog_error = None
    last_store_request = None
    for app in apps:
        try:
            if app["path"].is_relative_to("/System") and not app["store"]:
                status, available, detail = "SKIP", "", "App di sistema: aggiornamenti nel gruppo macOS / Apple"
            elif not app["bundle_id"]:
                status, available, detail = "SKIP", "", "Bundle ID assente: app censita, fonte aggiornamenti non identificabile"
            elif app["feed"] and not app["store"] and app["bundle_id"] not in cfg["cask_overrides"]:
                status, available, detail = sparkle_application(app, os_version, platform.machine())
            elif app["store"] and app["bundle_id"] not in cfg["cask_overrides"]:
                # Apple's public API is rate limited; space lookups across store apps.
                if last_store_request is not None:
                    time.sleep(max(0, 3.1 - (time.monotonic() - last_store_request)))
                last_store_request = time.monotonic()
                status, available, detail = store_application(app, cfg["app_store_country"], os_version)
            else:
                if by_id is None and catalog_error is None:
                    try:
                        by_id, by_token = cask_app_index(get_json("https://formulae.brew.sh/api/cask.json", max_bytes=64 * 1024 * 1024))
                    except CheckError as error:
                        catalog_error = error
                if catalog_error:
                    raise catalog_error
                token = cfg["cask_overrides"].get(app["bundle_id"])
                matches = [by_token[token]] if token and token in by_token else []
                if token and not matches:
                    raise CheckError("Cask configurato non trovato nel catalogo")
                if not token:
                    matches = [c for c, names in by_id.get(app["bundle_id"].casefold(), []) if app["path"].name.casefold() in names]
                if len(matches) == 1:
                    status, available, detail = cask_application(app, matches[0], os_version)
                else:
                    status, available, detail = "SKIP", "", "Nessuna fonte univoca: controllo manuale o cask_overrides per Bundle ID"
            item = row("Applicazioni", app["name"], status, app["version"], available, detail)
        except (CheckError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            item = row("Applicazioni", app["name"], "ERROR", app["version"],
                       detail=str(error) if isinstance(error, CheckError) else "Metadati app/catalogo non validi")
        item.update(app_path=str(app["path"]), bundle_id=app["bundle_id"], installed_build=app["build"])
        result.append(item)
    return result


def git_rows(cfg, root):
    """Inventory local source code without contacting any remote."""
    path = (root / cfg["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise CheckError("La directory Git deve essere interna al progetto FitHub")
    if not (path / ".git").exists():
        return [row("Git", "Sorgenti FitHub", "SKIP", detail="Repository Git assente nella directory configurata")]
    git = tool("git")
    if not git:
        return [row("Git", "Sorgenti FitHub", "SKIP", detail="Git non installato; aggiornamenti dei sorgenti non verificati")]
    head = operation("Git: lettura HEAD locale", lambda: command([git, "-C", str(path), "rev-parse", "HEAD"]))[0].strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise CheckError("Commit Git locale non riconosciuto")
    return [row("Git", "Sorgenti FitHub", "SKIP", head[:12],
                detail="Commit locale censito; aggiornamenti remoti non verificati, nessun fetch o accesso al server Git")]


def collect(cfg, root=PROJECT_ROOT):
    result = []
    groups = [(cfg["docker"]["enabled"], "Docker", lambda: docker_rows(cfg["docker"])),
              (cfg["docker_desktop"], "Docker Desktop", desktop_rows),
              (cfg["macos"], "macOS", macos_rows),
              (cfg["applications"]["enabled"], "Applicazioni", lambda: application_rows(cfg["applications"])),
              (cfg["homebrew"], "Homebrew", lambda: brew_rows(applications=result)),
              (cfg["git"]["enabled"], "Git", lambda: git_rows(cfg["git"], root))]
    for enabled, name, action in groups:
        if not enabled:
            result.append(row(name, name, "SKIP", detail="Disabilitato dalla configurazione"))
            continue
        print(f"Controllo {name}...", flush=True)
        try:
            result.extend(action())
        except CheckError as error:
            result.append(row(name, name, "ERROR", detail=str(error)))
        except (KeyError, TypeError, ValueError, AttributeError):
            # No tracebacks or arbitrary third-party messages in reports/logs.
            result.append(row(name, name, "ERROR", detail="Controllo non concluso: verificare rete, autorizzazioni e configurazione"))
    return result


def summary(rows, down_on_updates=True):
    errors = sum(x["status"] == "ERROR" for x in rows)
    updates = sum(x["status"] in ("UPDATE", "MAJOR", "REVIEW") for x in rows)
    checked = sum(x["status"] != "SKIP" for x in rows)
    skipped = len(rows) - checked
    if not checked:
        errors += 1
    code = 2 if errors else 1 if updates else 0
    status = "down" if errors or (updates and down_on_updates) else "up"
    message = f"Update check: {updates} aggiornamenti/revisioni, {errors} errori, {skipped} esclusi"
    applications = [x for x in rows if x.get("app_path")]
    unverified = sum(x["status"] == "SKIP" for x in applications)
    if applications:
        message += f"; {len(applications)} app censite, {unverified} non verificabili/separate"
    actionable = [x["name"] for x in rows if x["status"] in ("UPDATE", "MAJOR", "REVIEW", "ERROR")]
    if actionable:
        message += " | " + ", ".join(actionable[:4])[:250]
        if len(actionable) > 4:
            message += ", ..."
    return dict(status=status, exit_code=code, updates=updates, errors=errors,
                checked=checked, skipped=skipped, applications_found=len(applications),
                applications_unverified=unverified, message=message)


def atomic_write(path, data):
    if path.is_symlink():
        raise CheckError("File di stato/report non valido (link simbolico)")
    fd, name = tempfile.mkstemp(prefix=".updatecheck-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def normalize_push_url(value, source):
    """Accept a copied Push URL, never shell code or multiple lines/URLs."""
    def invalid(reason):
        raise CheckError(f"URL Push update checker non valido ({source}): {reason}")
    value = value.strip().lstrip("\ufeff").strip()
    wrappers = {'"': '"', "'": "'", "`": "`", "<": ">", "“": "”", "‘": "’"}
    for _ in range(2):
        if value and value[0] in wrappers and value.endswith(wrappers[value[0]]):
            value = value[1:-1].strip()
        else:
            break
    if not value:
        invalid("serve l'URL completo copiato dal monitor di tipo Push")
    if value.startswith("{\\rtf"):
        invalid("salvare il file come testo semplice UTF-8, non come documento RTF")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        invalid("inserire un solo URL su una sola riga, senza comandi o testo aggiuntivo")
    try:
        url = urllib.parse.urlsplit(value)
    except ValueError:
        invalid("host o indirizzo non interpretabile; ricopiare l'URL Push da Kuma")
    if url.scheme.lower() not in ("http", "https") or not url.netloc:
        invalid("serve un URL completo che inizi con http:// oppure https://")
    try:
        hostname, port = url.hostname, url.port
    except ValueError:
        invalid("host o porta non validi")
    if not hostname or (port is not None and not 1 <= port <= 65535):
        invalid("host o porta non validi")
    if url.username is not None or url.password is not None:
        invalid("usare l'URL Push senza credenziali utente:password nell'indirizzo")
    path = url.path.removesuffix("/")
    # Optional proxy path prefix; only the last component is the Push token.
    if not re.fullmatch(r"(?:/[A-Za-z0-9._~!$&'()*+,;=:@%+-]+)*/api/push/[A-Za-z0-9_-]+", path):
        invalid("il percorso deve terminare con /api/push/TOKEN; non basta l'indirizzo di Kuma o una chiave API")
    if any(c.isspace() or c in '"\\<>`{}' for c in url.netloc + path):
        invalid("spazi o caratteri aggiuntivi nell'indirizzo; ricopiare solo l'URL Push")
    # Discard copied status/msg/ping and fragments; the checker supplies its own result.
    return urllib.parse.urlunsplit((url.scheme.lower(), url.netloc, path, "", ""))


def push_url(script_dir=SCRIPT_DIR):
    value = os.environ.get("KUMA_UPDATECHECK_PUSH_URL", "").strip()
    source = "variabile KUMA_UPDATECHECK_PUSH_URL"
    path = script_dir / "updatecheck-push-url.txt"
    if path.is_symlink():
        raise CheckError("File URL Push non valido (link simbolico)")
    if not value and path.exists():
        source = "file updatecheck-push-url.txt"
        try:
            value = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            raise CheckError("updatecheck-push-url.txt non leggibile: usare un file di testo UTF-8 accessibile all'utente admin") from None
        if not value:
            raise CheckError("updatecheck-push-url.txt è vuoto: incollare l'URL completo del monitor Push")
    return normalize_push_url(value, source) if value else ""


def check_push_response(returncode, output):
    """Report only known categories, never raw bodies, headers or curl stderr."""
    body, separator, status = output.rpartition("\n")
    status = status.strip()
    if returncode not in (0, 22):
        reason = {3: "indirizzo non interpretabile da curl", 5: "DNS del proxy non risolto",
                  6: "DNS del dominio non risolto", 7: "connessione al server/proxy non riuscita",
                  16: "errore di comunicazione HTTP/2", 28: "timeout di connessione/risposta",
                  35: "negoziazione TLS non riuscita", 51: "certificato TLS non valido",
                  58: "certificato client non disponibile", 60: "certificato TLS non verificabile",
                  77: "archivio dei certificati CA non accessibile", 52: "risposta vuota dal server",
                  56: "connessione interrotta durante la risposta"}.get(returncode, "errore di trasporto")
        raise CheckError(f"Invio Kuma: {reason} (curl {returncode})")
    if not separator or not re.fullmatch(r"[1-5]\d\d", status):
        raise CheckError("Invio Kuma: nessuna risposta HTTP valida dal server/proxy")
    try:
        packet = json.loads(body.lstrip("\ufeff"))
    except (ValueError, TypeError):
        packet = None
    if (isinstance(packet, dict) and packet.get("ok") is False
            and packet.get("msg") in ("Monitor not found or not active.", "Monitor not found.")):
        raise CheckError(f"Invio Kuma: monitor Push non trovato o non attivo (HTTP {status}); ricopiare l'URL del monitor attivo")
    if status.startswith("3"):
        raise CheckError(f"Invio Kuma: reindirizzamento HTTP {status}; verificare URL finale e autenticazione del dominio pubblico")
    if status in ("401", "403"):
        raise CheckError(f"Invio Kuma: accesso negato HTTP {status}; verificare autenticazione e regole del proxy/dominio pubblico")
    if status == "404":
        raise CheckError("Invio Kuma: percorso Push non trovato (HTTP 404); verificare URL e instradamento del proxy")
    if status == "429":
        raise CheckError("Invio Kuma: troppe richieste (HTTP 429); riprovare dopo il limite imposto dal server/proxy")
    if status.startswith("5"):
        raise CheckError(f"Invio Kuma: errore del server/proxy (HTTP {status})")
    if not status.startswith("2") or returncode:
        raise CheckError(f"Invio Kuma: richiesta rifiutata (HTTP {status})")
    if not isinstance(packet, dict):
        html = body.lstrip().lower().startswith(("<!doctype html", "<html"))
        reason = "pagina HTML al posto della risposta API; possibile login/proxy o indirizzo errato" if html else "risposta JSON assente o non valida"
        raise CheckError(f"Invio Kuma: {reason} (HTTP {status})")
    if packet.get("ok") is not True:
        raise CheckError(f"Invio Kuma: il server non conferma ok=true (HTTP {status}); verificare il monitor Push e i log di Kuma")


def send_push(url, result):
    if not url:
        print("AVVISO: URL Kuma non configurato; report solo locale", file=sys.stderr)
        return False
    # Token only on stdin, never in process arguments or reports.
    # Retain error bodies to recognize Kuma's monitor-not-found response on HTTP 404.
    args = ["/usr/bin/curl", "-q", "--config", "-", "--globoff", "--silent", "--connect-timeout", "5",
            "--max-time", "15", "--get", "--data-urlencode", "status=" + result["status"],
            "--data-urlencode", "msg=" + result["message"], "--write-out", "\n%{http_code}"]
    try:
        response = subprocess.run(args, input='url = "' + url + '"\n', text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
    except subprocess.TimeoutExpired:
        raise CheckError("Invio Kuma: timeout del processo curl (oltre 20 secondi)") from None
    except OSError:
        raise CheckError("Invio Kuma: impossibile avviare /usr/bin/curl") from None
    check_push_response(response.returncode, response.stdout)
    return True


def save_report(report, log_dir, state_dir):
    result = report["result"]
    lines = ["FITHUB UPDATE CHECK - " + report["finished_at"], result["message"], ""]
    if report.get("push_error"):
        lines.extend(["Errore invio Kuma: " + report["push_error"], ""])
    for item in report["items"]:
        lines.extend([f'[{item["status"]}] {item["area"]} / {item["name"]}',
                      "  Installato: " + (item["installed"] or "-"),
                      "  Disponibile: " + (item["available"] or "-"), "  " + item["detail"], ""])
        if item.get("app_path"):
            lines[-1:-1] = ["  App: " + item["app_path"], "  Bundle ID: " + item.get("bundle_id", "")]
    name = "report-" + dt.datetime.fromisoformat(report["finished_at"]).strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid())
    data = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    atomic_write(log_dir / (name + ".json"), data)
    atomic_write(log_dir / (name + ".txt"), "\n".join(lines))
    atomic_write(log_dir / "latest.json", data)
    atomic_write(log_dir / "latest.txt", "\n".join(lines))
    atomic_write(state_dir / "last-result.json", data)


def run_scan(cfg, *, script_dir=SCRIPT_DIR, root=PROJECT_ROOT, no_push=False):
    os.umask(0o077)
    state_dir, log_dir = script_dir / "state", root / "logs/system/updatecheck"
    for path in [state_dir, log_dir]:
        if path.is_symlink():
            raise CheckError("Directory stato/report non valida (link simbolico)")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = state_dir / ".lock"
    if lock.is_symlink():
        raise CheckError("Lock non valido")
    with lock.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CheckError("Update checker già in esecuzione") from None
        url = "" if no_push else push_url(script_dir)
        started = dt.datetime.now().astimezone().isoformat()
        rows = collect(cfg, root)
        report = dict(started_at=started, finished_at=dt.datetime.now().astimezone().isoformat(),
                      items=rows, result=summary(rows, cfg["down_on_updates"]), kuma_sent=False)
        # Persist the real result even when Kuma is unavailable.
        save_report(report, log_dir, state_dir)
        push_failed = False
        if not no_push:
            try:
                report["kuma_sent"] = send_push(url, report["result"])
            except CheckError as error:
                push_failed = True
                report["push_error"] = str(error)
                print("ERRORE: " + str(error), file=sys.stderr)
        save_report(report, log_dir, state_dir)
        print(report["result"]["message"])
        print("Report: " + str(log_dir / "latest.txt"))
        return 2 if push_failed else report["result"]["exit_code"]


def retry_kuma(cfg, *, script_dir=SCRIPT_DIR, root=PROJECT_ROOT):
    """Resend a real, recent result; never manufacture an UP connectivity probe."""
    os.umask(0o077)
    state, logs = script_dir / "state", root / "logs/system/updatecheck"
    if state.is_symlink() or logs.is_symlink() or not state.is_dir() or not logs.is_dir():
        raise CheckError("Nessun risultato locale accessibile; eseguire prima una scansione completa")
    path, lock = state / "last-result.json", state / ".lock"
    if path.is_symlink() or lock.is_symlink():
        raise CheckError("Stato/lock non valido per il ritentativo Kuma")
    with lock.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CheckError("Update checker gia in esecuzione; attendere la fine prima di ritentare Kuma") from None
        try:
            report = json.loads(path.read_text())
            finished = dt.datetime.fromisoformat(report["finished_at"])
            items = report["items"]
            if (finished.tzinfo is None or not isinstance(items, list) or not items
                    or not all(isinstance(x, dict) and x.get("status") in ("OK", "UPDATE", "MAJOR", "REVIEW", "SKIP", "ERROR")
                               and all(isinstance(x.get(k), str) for k in ("name", "area", "installed", "available", "detail")) for x in items)):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise CheckError("Ultimo risultato locale non leggibile o non valido; eseguire una scansione completa") from None
        now = dt.datetime.now().astimezone()
        if not -60 <= (now - finished).total_seconds() <= 900:
            raise CheckError("Ultimo risultato oltre 15 minuti fa o data non valida; eseguire una nuova scansione completa")
        url = push_url(script_dir)
        if not url:
            raise CheckError("URL Kuma non configurato: salvare l'URL del monitor in updatecheck-push-url.txt")
        report["result"] = summary(items, cfg["down_on_updates"])
        outgoing = dict(report["result"], message="Scansione " + finished.strftime("%d/%m %H:%M:%S %z") + ": " + report["result"]["message"])
        report["kuma_attempt_at"] = now.isoformat()
        report["kuma_sent"] = False
        try:
            report["kuma_sent"] = send_push(url, outgoing)
        except CheckError as error:
            report["push_error"] = str(error)
            save_report(report, logs, state)
            raise
        report.pop("push_error", None)
        save_report(report, logs, state)
        print("Kuma: invio confermato; risultato della scansione " + report["finished_at"])
        print(report["result"]["message"])
        return report["result"]["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-push", action="store_true")
    parser.add_argument("--service", action="store_true")
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--retry-kuma", action="store_true", help="Riinvia il risultato reale di una scansione conclusa da non oltre 15 minuti")
    mode.add_argument("--diagnose-sources", action="store_true", help="Ricontrolla solo Docker e Git senza inviare a Kuma o riscrivere i report")
    args = parser.parse_args()
    try:
        runtime_check()
        if sys.platform != "darwin":
            raise CheckError("Eseguire l'update checker su macOS")
        cfg = configured()
        if args.diagnose_sources:
            diagnostic = dict(cfg, docker_desktop=False, macos=False, homebrew=False,
                              applications=dict(cfg["applications"], enabled=False))
            rows = [r for r in collect(diagnostic) if r["area"] in ("Docker", "Git")]
            print("Diagnosi servizi: fonti pubbliche e dati locali, nessun login, fetch Git o invio Kuma; report conservati.")
            for item in rows:
                print(f'[{item["status"]}] {item["area"]} / {item["name"]}')
                print("  Installato: " + (item["installed"] or "-"))
                print("  Disponibile: " + (item["available"] or "-"))
                print("  " + item["detail"])
            return 2 if any(r["status"] == "ERROR" for r in rows) else 0
        if args.retry_kuma:
            return retry_kuma(cfg)
        if args.self_check:
            url = push_url()
            print("Runtime e configurazione update checker validi")
            if url:
                source = "variabile KUMA_UPDATECHECK_PUSH_URL" if os.environ.get("KUMA_UPDATECHECK_PUSH_URL", "").strip() else "file updatecheck-push-url.txt"
                print("URL Push Kuma: formato valido (" + source + "); connessione non verificata")
            else:
                print("URL Push Kuma: non configurato; report solo locale")
            return 0
        return run_scan(cfg, no_push=args.no_push)
    except CheckError as error:
        print("ERRORE: " + str(error), file=sys.stderr)
    except (OSError, ValueError):
        print("ERRORE: stato/report non accessibili o dati non validi; verificare permessi e Privacy macOS", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
