"""Installer reali in un progetto temporaneo: launchctl e operazioni root simulati."""
import json
import os
from pathlib import Path
import plistlib
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1] / "scripts/monitormetrics"
INTERVALS = {"cpu": 15, "ram": 15, "ssd": 60, "swap": 30, "temperature": 30,
             "docker": 30, "backup": 60, "host": 30}


@unittest.skipUnless(sys.platform == "darwin", "Usa install e plutil di macOS")
class MonitorInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        # Verifica anche l'escaping XML e shell dei percorsi effettivi.
        self.project = self.base / "Fithub & monitor's project"
        self.scripts = self.project / "scripts/monitormetrics"
        self.scripts.mkdir(parents=True)
        self.logs = self.project / "logs/system/monitormetrics"
        self.plists = self.base / "LaunchDaemons"
        self.plists.mkdir()
        self.legacy = self.base / "legacy-scripts"
        self.legacy.mkdir()
        self.legacy_logs = self.base / "legacy-logs"
        self.legacy_logs.mkdir()
        self.legacy_status = self.base / "legacy-status"
        self.legacy_status.mkdir()
        self.events = self.base / "launchctl.jsonl"
        self.launchctl = self.base / "fake-launchctl"
        self.install = self.base / "fake-install"
        self.chown = self.base / "fake-chown"
        self.sudo = self.base / "unexpected-sudo"
        self.launchctl.write_text(
            f"#!{sys.executable}\nimport json, os, pathlib, plistlib, sys\n"
            f"p = pathlib.Path({str(self.events)!r})\n"
            "with p.open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1] == 'print':\n"
            "    import os\n"
            "    metric = sys.argv[2].rsplit('.', 1)[-1]\n"
            "    code = json.loads(os.environ.get('FITHUB_TEST_EXIT_CODES', '{}')).get(metric, 0)\n"
            "    print('state = not running\\nlast exit code = ' + str(code))\n"
            "    if code == 78: print('job state = spawn failed')\n"
            "if sys.argv[1] == 'bootstrap':\n"
            "    data = plistlib.loads(pathlib.Path(sys.argv[3]).read_bytes())\n"
            "    assert 'WorkingDirectory' not in data\n"
            "    assert 'StandardOutPath' not in data\n"
            "    assert 'StandardErrorPath' not in data\n"
            f"    assert pathlib.Path(data['ProgramArguments'][1]).parent == pathlib.Path({str(self.scripts)!r})\n"
            "    assert pathlib.Path(data['ProgramArguments'][1]).is_file()\n"
            "    metric = data['Label'].rsplit('.', 1)[-1]\n"
            "    if not os.environ.get('FITHUB_TEST_NO_SAMPLE'):\n"
            f"        with (pathlib.Path({str(self.logs)!r}) / (metric + '.log')).open('a') as f: f.write('campione simulato\\n')\n"
        )
        self.install.write_text(
            f"#!{sys.executable}\nimport subprocess, sys\n"
            "args = []; i = 1\n"
            "while i < len(sys.argv):\n"
            "    if sys.argv[i] in ('-o', '-g'): i += 2; continue\n"
            "    args.append(sys.argv[i]); i += 1\n"
            "sys.exit(subprocess.run(['/usr/bin/install', *args]).returncode)\n"
        )
        self.chown.write_text("#!/bin/sh\nexit 0\n")
        self.sudo.write_text("#!/bin/sh\necho 'SUDO INATTESO' >&2\nexit 99\n")
        for path in [self.launchctl, self.install, self.chown, self.sudo]:
            path.chmod(0o700)
        for metric in INTERVALS:
            shutil.copyfile(ROOT / f"{metric}.sh", self.scripts / f"{metric}.sh")
            self.write_url(metric)
        for path in ROOT.glob("install-*-service*.sh"):
            self.write_installer(path)

    def write_installer(self, path):
        source = path.read_text()
        # Le sole sostituzioni di privilegi/servizi sono nella copia di test.
        source = source.replace("if (( EUID != 0 )); then", "if false; then")
        source = source.replace("/bin/sleep 1", ":")
        for original, replacement in [
            ("/Library/Application Support/FitHub/monitormetrics", self.legacy),
            ("/Library/Application Support/FitHub/backup-status", self.legacy_status),
            ("/Library/Logs/FitHub", self.legacy_logs),
            ("/Library/LaunchDaemons", self.plists),
        ]:
            source = source.replace(original, str(replacement))
        for original, replacement in [("/usr/bin/install", self.install),
                                      ("/usr/sbin/chown", self.chown),
                                      ("/bin/launchctl", self.launchctl),
                                      ("/usr/bin/sudo", self.sudo)]:
            source = source.replace(original, shlex.quote(str(replacement)))
        (self.scripts / path.name).write_text(source)

    def write_url(self, metric, content=None):
        path = self.scripts / f"{metric}-push-url.txt"
        path.write_text(content or f"https://kuma.example.test/api/push/TOKEN_{metric}\n")
        return path

    def run_installer(self, metric, exit_codes=None, no_sample=False):
        name = "install-all-services.sh" if metric == "all" else f"install-{metric}-service.sh"
        return subprocess.run(["/bin/bash", str(self.scripts / name)],
                              env=dict(os.environ, SUDO_USER=pwd.getpwuid(os.getuid()).pw_name,
                                       FITHUB_TEST_EXIT_CODES=json.dumps(exit_codes or {}),
                                       FITHUB_TEST_NO_SAMPLE="1" if no_sample else ""),
                              text=True, capture_output=True, timeout=15)

    def read_plist(self, metric):
        return plistlib.loads((self.plists / f"ch.nmperformance.fithub.{metric}.plist").read_bytes())

    def calls(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_all_services_run_canonical_scripts_with_project_logs(self):
        for metric, interval in INTERVALS.items():
            with self.subTest(metric=metric):
                result = self.run_installer(metric)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                plist = self.read_plist(metric)
                self.assertEqual(plist["ProgramArguments"],
                                 ["/bin/bash", str(self.scripts / f"{metric}.sh"), "--service"])
                self.assertEqual(plist["StartInterval"], interval)
                self.assertNotIn("WorkingDirectory", plist)
                self.assertNotIn("StandardOutPath", plist)
                self.assertNotIn("StandardErrorPath", plist)
                self.assertEqual((self.scripts / f"{metric}.sh").stat().st_mode & 0o777, 0o700)
                self.assertEqual((self.scripts / f"{metric}-push-url.txt").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len([c for c in self.calls() if c[0] == "bootstrap"]), 8)
        self.assertEqual(list(self.legacy.iterdir()), [])

    def test_migration_recovers_url_and_logs_without_removing_old_files(self):
        (self.scripts / "host-push-url.txt").unlink()
        token = "https://kuma.example.test/api/push/PRIVATE_LEGACY_TOKEN\n"
        (self.legacy / "host-push-url.txt").write_text(token)
        (self.legacy_logs / "host.log").write_text("precedente heartbeat\n")
        (self.legacy_logs / "host-error.log").write_text("precedente errore\n")
        result = self.run_installer("host")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.scripts / "host-push-url.txt").read_text(), token)
        self.assertTrue((self.logs / "host.log").read_text().startswith("precedente heartbeat\n"))
        self.assertEqual((self.logs / "host-error.log").read_text(), "precedente errore\n")
        self.assertTrue((self.legacy / "host-push-url.txt").exists())
        self.assertNotIn("PRIVATE_LEGACY_TOKEN", result.stdout + result.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["print", "bootout", "enable", "bootstrap", "print"])

    def test_reinstall_preserves_canonical_tokens_logs_and_shared_parent_permissions(self):
        self.logs.mkdir(parents=True)
        parent = self.project / "logs/system"
        parent.chmod(0o755)
        (self.logs / "host.log").write_text("log attuale\n")
        (self.legacy_logs / "host.log").write_text("log vecchio\n")
        (self.legacy / "host-push-url.txt").write_text("https://kuma.example.test/api/push/OLD\n")
        expected = (self.scripts / "host-push-url.txt").read_text()
        for index in range(2):
            result = self.run_installer("host")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.scripts / "host-push-url.txt").read_text(), expected)
            self.assertEqual((self.logs / "host.log").read_text(),
                             "log attuale\n" + "campione simulato\n" * (index + 1))
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)

    def test_swap_migrates_history_then_preserves_it_and_the_lock(self):
        old = self.legacy / "swap-state"
        old.mkdir()
        (old / "history").write_text("1 100 200 300\n")
        result = self.run_installer("swap")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.scripts / "swap-state"
        self.assertEqual((state / "history").read_text(), "1 100 200 300\n")
        (state / "history").write_text("1 100 400 500\n")
        (state / ".lock").touch()
        lock_inode = (state / ".lock").stat().st_ino
        result = self.run_installer("swap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((state / "history").read_text(), "1 100 400 500\n")
        self.assertEqual((state / ".lock").stat().st_ino, lock_inode)

    def test_backup_imports_minimal_status_and_preserves_existing_canonical_state(self):
        store = self.project / "backups"
        store.mkdir()
        daily = {"ok": True, "finished_at": "2026-10-26T03:00:10.331246+01:00", "log": "PRIVATE_LOG"}
        weekly = {"ok": False, "finished_at": "2026-10-25T03:00:00+01:00"}
        monthly = {"ok": True, "finished_at": "2026-10-26T05:00:00+01:00"}
        (store / "status-daily.json").write_text(json.dumps(daily))
        (self.legacy_status / "status-weekly.json").write_text(json.dumps(weekly))
        (self.scripts / "status-monthly.json").write_text(json.dumps(monthly))
        result = self.run_installer("backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        for kind, record in [("daily", daily), ("weekly", weekly), ("monthly", monthly)]:
            path = self.scripts / f"status-{kind}.json"
            self.assertEqual(json.loads(path.read_text()), {k: record[k] for k in ["ok", "finished_at"]})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_configuration_does_not_stop_existing_service(self):
        self.write_url("host", "https://kuma.example.test/api/push/TOKEN\"bad\n")
        result = self.run_installer("host")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("TOKEN", result.stderr + result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.logs.exists())

    def test_install_all_migrates_every_service(self):
        result = self.run_installer("all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        labels = [self.read_plist(m)["Label"] for m in INTERVALS]
        calls = self.calls()
        self.assertEqual([c[1] for c in calls if c[0] == "enable"], ["system/" + l for l in labels])
        self.assertEqual(len([c for c in calls if c[0] == "bootstrap"]), 8)

    def test_install_all_validates_last_monitor_before_any_service_is_stopped(self):
        self.write_url("host", "not a push URL\n")
        result = self.run_installer("all")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.logs.exists())

    def test_spawn_failure_is_not_reported_as_installed_successfully(self):
        result = self.run_installer("cpu", {"cpu": 78})
        self.assertEqual(result.returncode, 2)
        self.assertIn("spawn failed", result.stderr)
        self.assertNotIn("lettura e invio riusciti", result.stdout)

    def test_application_down_is_a_running_sensor_but_io_error_is_failure(self):
        result = self.run_installer("ram", {"ram": 1})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("condizione DOWN", result.stdout)
        result = self.run_installer("host", {"host": 2})
        self.assertEqual(result.returncode, 2)
        self.assertIn("codice 2", result.stderr)

    def test_install_all_continues_other_services_after_one_failed_start(self):
        result = self.run_installer("all", {"cpu": 78})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len([c for c in self.calls() if c[0] == "bootstrap"]), 8)
        self.assertIn("verifica non riuscita per: cpu", result.stderr)

    def test_exit_zero_without_new_sample_is_not_reported_as_healthy(self):
        result = self.run_installer("host", no_sample=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("non verificato", result.stderr)
        self.assertNotIn("lettura e invio riusciti", result.stdout)


if __name__ == "__main__":
    unittest.main()
