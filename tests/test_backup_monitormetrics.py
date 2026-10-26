"""Backup monitor: JSON reali letti da Foundation; tempo e Kuma simulati."""
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import grp
import pwd
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1] / "scripts/monitormetrics"
NOW = int(dt.datetime(2026, 10, 26, 18, tzinfo=dt.timezone.utc).timestamp())
URL = "https://kuma.example.test/api/push/TOKEN_BACKUP_FITTIZIO_TEST"


@unittest.skipUnless(sys.platform == "darwin", "Usa Foundation, bash e plutil di macOS")
class BackupMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = self.root / "status"
        self.status.mkdir()
        self.record = self.root / "request.json"
        self.curl = self.root / "fake-curl"
        self.date = self.root / "fake-date"
        self.script = self.root / "backup.sh"
        self.date.write_text("#!/bin/sh\nif [ \"$1\" = '+%s' ]; then echo " + str(NOW) + "; else echo TEST_TIME; fi\n")
        self.date.chmod(0o700)
        source = (ROOT / "backup.sh").read_text()
        source = source.replace('STATUS_DIR="$SCRIPT_DIR"', "STATUS_DIR=" + shlex.quote(str(self.status)))
        source = source.replace("/usr/bin/curl", shlex.quote(str(self.curl)))
        source = source.replace("/bin/date", shlex.quote(str(self.date)))
        self.script.write_text(source)
        self.set_response('{"ok":true}\n200')

    def set_response(self, response, code=0):
        self.curl.write_text(
            f"#!{sys.executable}\nimport json, pathlib, sys\n"
            f"pathlib.Path({str(self.record)!r}).write_text(json.dumps(\n"
            "    {'config': sys.stdin.read(), 'args': sys.argv[1:]}))\n"
            f"sys.stdout.write({response!r})\nsys.exit({code})\n"
        )
        self.curl.chmod(0o700)

    def write_status(self, kind="daily", *, age=1, ok=True, stamp=None):
        if stamp is None:
            stamp = dt.datetime.fromtimestamp(NOW - age * 3600, dt.timezone.utc).isoformat(timespec="microseconds")
        record = {"ok": ok, "finished_at": stamp, "error": "PRIVATE_ERROR", "log": "/PRIVATE_LOG"}
        (self.status / f"status-{kind}.json").write_text(json.dumps(record))

    def run_script(self, *args, url=None):
        env = dict(os.environ)
        env.pop("KUMA_BACKUP_PUSH_URL", None)
        if url is not None:
            env["KUMA_BACKUP_PUSH_URL"] = url
        return subprocess.run(["/bin/bash", str(self.script), *args], env=env,
                              text=True, capture_output=True, timeout=5)

    def push_status(self):
        return next(a for a in json.loads(self.record.read_text())["args"] if a.startswith("status="))

    def run_import(self, source_rows, existing=None, symlink=False):
        project = self.root / "Fithub"
        scripts = project / "scripts/monitormetrics"
        scripts.mkdir(parents=True)
        source_dir = project / "backups"
        source_dir.mkdir()
        for kind, record in source_rows.items():
            (source_dir / f"status-{kind}.json").write_text(json.dumps(record))
        staging = self.root / "staging"
        staging.mkdir()
        destination = self.root / "imported"
        destination.mkdir()
        if existing is not None:
            (destination / "status-daily.json").write_text(json.dumps(existing))
        if symlink:
            (destination / "status-daily.json").symlink_to(source_dir / "status-daily.json")
        source = (ROOT / "install-backup-service.sh").read_text()
        block = source.split("# Il produttore pubblica", 1)[1].split('/usr/bin/install -o root', 1)[0]
        block = "# Il produttore pubblica" + block
        block = block.replace('STATUS_DIR="$SCRIPT_DIR"',
                              "STATUS_DIR=" + shlex.quote(str(destination)))
        block = block.replace('/Library/Application Support/FitHub/backup-status/',
                              str(self.root / "legacy-status") + '/')
        env = dict(os.environ, SCRIPT_DIR=str(scripts), PROJECT_ROOT=str(project), STAGING=str(staging),
                   SERVICE_USER=pwd.getpwuid(os.getuid()).pw_name,
                   SERVICE_GROUP=grp.getgrgid(os.getgid()).gr_name)
        result = subprocess.run(["/bin/bash", "-euc", block], env=env,
                                text=True, capture_output=True, timeout=5)
        return result, destination

    def test_native_json_reader_age_boundaries(self):
        for age, code, marker in [(1, 0, "OK"), (30, 0, "OK"),
                                   (30 + 1 / 3600, 0, "AVVISO"), (36, 0, "AVVISO"),
                                   (36 + 1 / 3600, 1, "CRITICO")]:
            with self.subTest(age=age):
                self.write_status(age=age)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(marker, result.stdout)
                self.assertEqual(self.push_status(), "status=down" if code else "status=up")
                self.assertNotIn("PRIVATE", result.stdout + json.dumps(json.loads(self.record.read_text())["args"]))

    def test_latest_attempt_failure_and_later_success_recovery(self):
        self.write_status("daily", age=2)
        self.write_status("weekly", age=1, ok=False)
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 1)
        self.assertIn("weekly FALLITO", result.stdout)
        self.write_status("daily", age=0.5)
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Backup daily:", result.stdout)

    def test_monthly_counts_but_verify_sync_and_init_do_not(self):
        for kind in ["verify", "sync", "init-local"]:
            self.write_status(kind, age=0)
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nessun backup registrato", result.stdout)
        self.write_status("monthly", age=1)
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Backup monthly:", result.stdout)

    def test_timezone_microseconds_and_equivalent_time_failure_tie(self):
        stamp = dt.datetime.fromtimestamp(NOW - 3600, dt.timezone(dt.timedelta(hours=1))).isoformat(timespec="microseconds")
        self.write_status("daily", stamp=stamp)
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("eta 1.0h", result.stdout)
        utc = dt.datetime.fromtimestamp(NOW - 3600, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        self.write_status("weekly", stamp=utc, ok=False)
        self.assertEqual(self.run_script("--dry-run").returncode, 1)

    def test_missing_and_invalid_data_never_report_up(self):
        self.assertEqual(self.run_script("--dry-run").returncode, 1)
        self.write_status()
        for content in ["bad JSON", "{}", '{"ok":"true","finished_at":"2026-10-26T17:00:00Z"}']:
            (self.status / "status-weekly.json").write_text(content)
            result = self.run_script("--once", url=URL)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(self.push_status(), "status=down")

    def test_bad_dates_and_future_timestamps(self):
        for stamp in ["2026-10-26T17:00:00", "2026-02-30T17:00:00Z",
                      "2026-13-01T17:00:00Z", "2026-10-26T25:00:00Z",
                      "2026-10-26T17:00:00+25:00", "2026-10-26T18:06:00Z", "not a date"]:
            with self.subTest(stamp=stamp):
                self.write_status(stamp=stamp)
                self.assertEqual(self.run_script("--dry-run").returncode, 2)
        self.write_status(stamp="2026-10-26T18:02:00Z")
        self.assertEqual(self.run_script("--dry-run").returncode, 0)

    def test_reader_command_failure(self):
        failed = self.root / "failed-reader"
        failed.write_text("#!/bin/sh\nexit 1\n")
        failed.chmod(0o700)
        self.script.write_text(self.script.read_text().replace("/usr/bin/osascript", shlex.quote(str(failed))))
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.push_status(), "status=down")

    def test_dry_run_does_not_send_or_change_status(self):
        self.write_status()
        original = (self.status / "status-daily.json").read_bytes()
        self.assertEqual(self.run_script("--dry-run", url=URL).returncode, 0)
        self.assertFalse(self.record.exists())
        self.assertEqual((self.status / "status-daily.json").read_bytes(), original)

    def test_push_query_replacement_and_privacy(self):
        self.write_status()
        result = self.run_script("--once", url=URL + "?status=down&msg=OLD&ping=99")
        self.assertEqual(result.returncode, 0)
        request = json.loads(self.record.read_text())
        self.assertEqual(request["config"], f'url = "{URL}"\n')
        self.assertTrue(any(a.startswith("msg=Backup daily:") for a in request["args"]))
        self.assertNotIn("TOKEN_BACKUP", json.dumps(request["args"]) + result.stdout + result.stderr)
        self.assertNotIn("PRIVATE", json.dumps(request["args"]) + result.stdout + result.stderr)
        self.assertNotIn("ping=", json.dumps(request))

    def test_url_file_precedence_missing_url_and_arguments(self):
        self.write_status()
        file = self.root / "backup-push-url.txt"
        file.write_bytes((URL + "\r\n").encode())
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.assertEqual(self.run_script("--once", url=URL + "OTHER").returncode, 0)
        self.assertIn("OTHER", json.loads(self.record.read_text())["config"])
        file.write_text("")
        self.assertEqual(self.run_script("--once").returncode, 2)
        file.unlink()
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("solo lettura locale", result.stderr)
        self.assertEqual(self.run_script("--once", url="bad").returncode, 2)
        self.assertEqual(self.run_script("--invalid").returncode, 2)

    def test_push_failure_is_error_even_if_backup_already_failed(self):
        self.write_status(ok=False)
        for response, code in [('{"ok":false}\n200', 0), ('<html>Login</html>\n200', 0),
                               ('{"ok":true}\n302', 0), ('', 7)]:
            self.set_response(response, code)
            self.assertEqual(self.run_script("--once", url=URL).returncode, 2)

    def test_loop_continues_every_60s_after_down(self):
        self.write_status(age=40)
        sleep = self.root / "fake-sleep"
        delay = self.root / "delay"
        sleep.write_text("#!/bin/sh\nprintf '%s' \"$1\" > " + shlex.quote(str(delay)) + "\nexit 17\n")
        sleep.chmod(0o700)
        self.script.write_text(self.script.read_text().replace("/bin/sleep", shlex.quote(str(sleep))))
        result = self.run_script(url=URL)
        self.assertEqual(result.returncode, 17)
        self.assertIn("ogni 60s", result.stdout)
        self.assertIn("CRITICO", result.stdout)
        self.assertGreaterEqual(int(delay.read_text()), 57)
        self.assertLessEqual(int(delay.read_text()), 60)

    def test_installer_configuration_and_metadata_directory(self):
        installer = (ROOT / "install-backup-service.sh").read_text()
        xml = installer.split("<<'PLIST_XML'\n", 1)[1].split('\nPLIST_XML', 1)[0]
        actual = plistlib.loads(xml.encode())
        expected = plistlib.loads((ROOT / "ch.nmperformance.fithub.backup.plist").read_bytes())
        del expected["UserName"]
        self.assertEqual(actual, expected)
        self.assertEqual(actual["StartInterval"], 60)
        self.assertEqual(actual["Label"], "ch.nmperformance.fithub.backup")
        self.assertEqual(actual["ProgramArguments"][-1], "--service")
        self.assertIn('STATUS_DIR="$SCRIPT_DIR"', installer)
        self.assertNotIn("WorkingDirectory", actual)

    def test_importer_sanitizes_metadata_and_rejects_string_booleans(self):
        stamp = "2026-10-26T17:00:00.123456+01:00"
        result, destination = self.run_import({
            "daily": {"ok": True, "finished_at": stamp, "log": "PRIVATE"},
            "weekly": {"ok": False, "finished_at": stamp, "error": "PRIVATE"},
            "monthly": {"ok": "true", "finished_at": stamp},
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        for kind, ok in [("daily", True), ("weekly", False)]:
            path = destination / f"status-{kind}.json"
            self.assertEqual(json.loads(path.read_text()), {"ok": ok, "finished_at": stamp})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((destination / "status-monthly.json").exists())
        self.assertIn("monthly non importabile", result.stderr)

    def test_importer_preserves_existing_metadata(self):
        existing = {"ok": False, "finished_at": "2026-10-26T17:00:00+01:00"}
        result, destination = self.run_import({"daily": {
            "ok": True, "finished_at": "2026-10-25T17:00:00+01:00"}}, existing=existing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((destination / "status-daily.json").read_text()), existing)

    def test_importer_rejects_symbolic_link_target(self):
        result, _ = self.run_import({"daily": {
            "ok": True, "finished_at": "2026-10-26T17:00:00+01:00"}}, symlink=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("link simbolici", result.stderr)


if __name__ == "__main__":
    unittest.main()
