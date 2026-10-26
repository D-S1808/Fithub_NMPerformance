"""Heartbeat Host: boot, orologio e Push simulati; nessun servizio installato."""
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1] / "scripts/monitormetrics"
TOKEN = "TOKEN_HOST_FITTIZIO_TEST"
URL = f"https://kuma.example.test/api/push/{TOKEN}"
BOOT = 1_790_000_000


@unittest.skipUnless(sys.platform == "darwin", "Usa bash e plutil di macOS")
class HostMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.root / "request.json"
        self.sysctl = self.root / "fake-sysctl"
        self.date = self.root / "fake-date"
        self.curl = self.root / "fake-curl"
        self.script = self.root / "host.sh"
        source = (ROOT / "host.sh").read_text()
        for original, fake in [("/usr/sbin/sysctl", self.sysctl),
                               ("/bin/date", self.date), ("/usr/bin/curl", self.curl)]:
            source = source.replace(original, shlex.quote(str(fake)))
        self.script.write_text(source)
        self.set_boot()
        self.set_now(BOOT + 60)
        self.set_response('{"ok":true}\n200')

    def set_boot(self, value=None, code=0):
        if value is None:
            value = f"{{ sec = {BOOT}, usec = 123456 }} Mon Sep 21 00:00:00 2026"
        self.sysctl.write_text(
            "#!/bin/sh\n[ \"$*\" = '-n kern.boottime' ] || exit 91\n"
            "printf '%s' " + shlex.quote(value) + f"\nexit {code}\n"
        )
        self.sysctl.chmod(0o700)

    def set_now(self, value, code=0):
        self.date.write_text(
            "#!/bin/sh\nif [ \"$1\" = '+%s' ]; then\nprintf '%s' "
            + shlex.quote(str(value)) + f"\nexit {code}\nfi\n"
            "printf '%s' '2026-10-26 20:00:00 CET'\n"
        )
        self.date.chmod(0o700)

    def set_response(self, response, code=0):
        self.curl.write_text(
            f"#!{sys.executable}\nimport json, pathlib, sys\n"
            f"pathlib.Path({str(self.record)!r}).write_text(json.dumps(\n"
            "    {'config': sys.stdin.read(), 'args': sys.argv[1:]}))\n"
            f"sys.stdout.write({response!r})\nsys.exit({code})\n"
        )
        self.curl.chmod(0o700)

    def run_script(self, *args, url=None):
        env = dict(os.environ)
        env.pop("KUMA_HOST_PUSH_URL", None)
        if url is not None:
            env["KUMA_HOST_PUSH_URL"] = url
        return subprocess.run(["/bin/bash", str(self.script), *args], env=env,
                              text=True, capture_output=True, timeout=5)

    def test_uptime_boundaries_and_reboot(self):
        for elapsed, expected in [(0, "0g 00h 00m 00s"), (59, "0g 00h 00m 59s"),
                                  (60, "0g 00h 01m 00s"), (3599, "0g 00h 59m 59s"),
                                  (3600, "0g 01h 00m 00s"), (86400, "1g 00h 00m 00s"),
                                  (184448, "2g 03h 14m 08s"), (3, "0g 00h 00m 03s")]:
            with self.subTest(elapsed=elapsed):
                self.set_now(BOOT + elapsed)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Host online - uptime {expected}", result.stdout)
                self.assertIn("status=up", json.loads(self.record.read_text())["args"])

    def test_invalid_boot_or_failed_command_sends_down(self):
        for boot, code in [("", 0), ("garbage", 0), ("{ sec = 0, usec = 0 }", 0),
                           ("{ sec = -1, usec = 0 }", 0),
                           ("{ sec = 9999999999999, usec = 0 }", 0),
                           ("{ sec = 01790000000, usec = 0 }", 0),
                           (f"{{ sec = {BOOT}, usec = 0 }}", 1)]:
            with self.subTest(boot=boot, code=code):
                self.set_boot(boot, code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Host online", result.stdout)
                self.assertIn("status=down", json.loads(self.record.read_text())["args"])

    def test_invalid_clock_or_boot_in_future_sends_down(self):
        for now, code in [("", 0), ("NaN", 0), ("-1", 0), ("1.5", 0),
                          ("01790000000", 0), ("9999999999999", 0),
                          (BOOT - 1, 0), (BOOT + 60, 1)]:
            with self.subTest(now=now, code=code):
                self.set_now(now, code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertIn("lettura uptime non riuscita", result.stdout)
                self.assertIn("status=down", json.loads(self.record.read_text())["args"])

    def test_reading_recovers_after_failure(self):
        self.set_boot("unavailable", code=1)
        self.assertEqual(self.run_script("--once", url=URL).returncode, 2)
        self.set_boot()
        self.assertEqual(self.run_script("--once", url=URL).returncode, 0)
        self.assertIn("status=up", json.loads(self.record.read_text())["args"])

    def test_dry_run_ignores_url_and_does_not_send(self):
        (self.root / "host-push-url.txt").write_text("invalid")
        result = self.run_script("--dry-run", url="invalid")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Host online", result.stdout)
        self.assertFalse(self.record.exists())

    def test_push_query_encoding_and_token_privacy(self):
        result = self.run_script("--once", url=URL + "?status=down&msg=OLD&ping=99")
        self.assertEqual(result.returncode, 0)
        request = json.loads(self.record.read_text())
        self.assertEqual(request["config"], f'url = "{URL}"\n')
        self.assertIn("--data-urlencode", request["args"])
        self.assertIn("msg=Host online - uptime 0g 00h 01m 00s", request["args"])
        self.assertNotIn("ping=", json.dumps(request))
        self.assertNotIn(TOKEN, json.dumps(request["args"]) + result.stdout + result.stderr)

    def test_url_file_and_environment_override(self):
        file = self.root / "host-push-url.txt"
        file.write_bytes((URL + "\r\n").encode())
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.assertEqual(self.run_script("--once", url=URL + "OTHER").returncode, 0)
        self.assertIn("OTHER", json.loads(self.record.read_text())["config"])
        file.write_text("")
        self.assertEqual(self.run_script("--once").returncode, 2)

    def test_invalid_urls_are_rejected_without_token_exposure(self):
        for url in ["bad-url", URL + "#fragment", URL + '"',
                    URL.replace("kuma", "user@kuma"), URL + "\\x"]:
            with self.subTest(url=url):
                result = self.run_script("--once", url=url)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)
                self.assertFalse(self.record.exists())

    def test_no_url_and_arguments(self):
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("solo lettura locale", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.run_script("--unknown").returncode, 2)
        self.assertEqual(self.run_script("--once", "--dry-run").returncode, 2)
        self.assertEqual(self.run_script("--help").returncode, 0)

    def test_rejected_pushes_report_error(self):
        for response, code in [('{"ok":false}\n200', 0), ('<html>Login</html>\n200', 0),
                               ('{"ok":true}\n302', 0), ('', 7)]:
            with self.subTest(response=response):
                self.set_response(response, code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertIn("invio a Uptime Kuma fallito", result.stderr)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_continuous_mode_every_30s_survives_read_and_push_errors(self):
        sleep = self.root / "fake-sleep"
        delay = self.root / "delay"
        sleep.write_text("#!/bin/sh\nprintf '%s' \"$1\" > " + shlex.quote(str(delay)) + "\nexit 17\n")
        sleep.chmod(0o700)
        self.script.write_text(self.script.read_text().replace("/bin/sleep", shlex.quote(str(sleep))))
        for read_error, push_error in [(False, False), (True, False), (False, True)]:
            with self.subTest(read_error=read_error, push_error=push_error):
                self.set_boot("unavailable" if read_error else None, int(read_error))
                self.set_response("" if push_error else '{"ok":true}\n200', 7 if push_error else 0)
                result = self.run_script(url=URL)
                self.assertEqual(result.returncode, 17)
                self.assertIn("ogni 30s", result.stdout)
                self.assertGreaterEqual(int(delay.read_text()), 27)
                self.assertLessEqual(int(delay.read_text()), 30)

    def test_installer_configuration_every_30s(self):
        installer = (ROOT / "install-host-service.sh").read_text()
        xml = installer.split("<<'PLIST_XML'\n", 1)[1].split('\nPLIST_XML', 1)[0]
        actual = plistlib.loads(xml.encode())
        expected = plistlib.loads((ROOT / "ch.nmperformance.fithub.host.plist").read_bytes())
        del expected["UserName"]
        self.assertEqual(actual, expected)
        self.assertEqual(actual["StartInterval"], 30)
        self.assertTrue(actual["RunAtLoad"])
        self.assertEqual(actual["ProgramArguments"],
                         ["/bin/bash", "/Users/admin/Documents/Fithub/scripts/monitormetrics/host.sh", "--service"])
        self.assertEqual(actual["Label"], "ch.nmperformance.fithub.host")
        self.assertNotIn("temperature", installer)


if __name__ == "__main__":
    unittest.main()
