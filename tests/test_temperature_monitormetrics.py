"""Temperatura: stati termici e Push simulati, nessuna modifica ai servizi."""
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
TOKEN = "TOKEN_TEMPERATURE_FITTIZIO_TEST"
URL = f"https://kuma.example.test/api/push/{TOKEN}"


@unittest.skipUnless(sys.platform == "darwin", "Usa bash e plutil di macOS")
class TemperatureMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.root / "request.json"
        self.osascript = self.root / "fake-osascript"
        self.curl = self.root / "fake-curl"
        self.script = self.root / "temperature.sh"
        source = (ROOT / "temperature.sh").read_text()
        source = source.replace("/usr/bin/osascript", shlex.quote(str(self.osascript)))
        source = source.replace("/usr/bin/curl", shlex.quote(str(self.curl)))
        self.script.write_text(source)
        self.set_state("0")
        self.set_response('{"ok":true}\n200')

    def set_state(self, state, code=0):
        self.osascript.write_text(
            "#!/bin/sh\nprintf '%s' " + shlex.quote(state) + f"\nexit {code}\n"
        )
        self.osascript.chmod(0o700)

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
        env.pop("KUMA_TEMPERATURE_PUSH_URL", None)
        if url is not None:
            env["KUMA_TEMPERATURE_PUSH_URL"] = url
        return subprocess.run(["/bin/bash", str(self.script), *args], env=env,
                              text=True, capture_output=True, timeout=5)

    def test_all_states_and_recovery(self):
        for state, name, status, code, marker in [
            ("0", "nominal", "up", 0, "OK"),
            ("1", "fair", "up", 0, "AVVISO"),
            ("2", "serious", "down", 1, "CRITICO"),
            ("3", "critical", "down", 1, "CRITICO"),
            ("1", "fair", "up", 0, "AVVISO"),
            ("0", "nominal", "up", 0, "OK"),
        ]:
            with self.subTest(state=state):
                self.set_state(state)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(f"Stato termico {name}", result.stdout)
                self.assertIn(marker, result.stdout)
                self.assertIn(f"status={status}", json.loads(self.record.read_text())["args"])
                self.assertNotIn("°C", result.stdout)

    def test_invalid_or_unknown_states_fail_closed(self):
        for state in ["", "4", "-1", "NaN", "undefined", "0.0", "nominal", "0\nnoise"]:
            with self.subTest(state=state):
                self.set_state(state)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertIn("lettura dello stato termico non riuscita", result.stdout)
                self.assertIn("status=down", json.loads(self.record.read_text())["args"])

    def test_command_failure_does_not_report_nominal(self):
        self.set_state("0", code=1)
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("nominal", result.stdout)
        self.assertIn("status=down", json.loads(self.record.read_text())["args"])

    def test_dry_run_is_local_even_with_url(self):
        (self.root / "temperature-push-url.txt").write_text(URL)
        result = self.run_script("--dry-run", url=URL)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Stato termico nominal - OK", result.stdout)
        self.assertFalse(self.record.exists())

    def test_push_query_encoding_and_token_privacy(self):
        result = self.run_script("--once", url=URL + "?status=down&msg=OLD&ping=99")
        self.assertEqual(result.returncode, 0)
        request = json.loads(self.record.read_text())
        self.assertEqual(request["config"], f'url = "{URL}"\n')
        self.assertIn("--data-urlencode", request["args"])
        self.assertIn("msg=Stato termico nominal - OK", request["args"])
        self.assertNotIn("ping=", json.dumps(request))
        self.assertNotIn(TOKEN, json.dumps(request["args"]) + result.stdout + result.stderr)

    def test_url_file_environment_override_and_blank_file(self):
        file = self.root / "temperature-push-url.txt"
        file.write_bytes((URL + "\r\n").encode())
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.assertEqual(self.run_script("--once", url=URL + "OTHER").returncode, 0)
        self.assertIn("OTHER", json.loads(self.record.read_text())["config"])
        file.write_text("")
        self.assertEqual(self.run_script("--once").returncode, 2)

    def test_no_url_warns_and_arguments_are_validated(self):
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("solo lettura locale", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.run_script("--once", url="bad-url").returncode, 2)
        self.assertEqual(self.run_script("--unknown").returncode, 2)
        self.assertEqual(self.run_script("--once", "--dry-run").returncode, 2)

    def test_rejected_pushes_override_critical_exit_code(self):
        self.set_state("3")
        for response, code in [('{"ok":false}\n200', 0), ('<html>Login</html>\n200', 0),
                               ('{"ok":true}\n302', 0), ('', 7)]:
            with self.subTest(response=response):
                self.set_response(response, code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertIn("CRITICO", result.stdout)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_continuous_mode_every_30s_survives_critical_and_network_errors(self):
        self.set_state("2")
        sleep = self.root / "fake-sleep"
        delay = self.root / "delay"
        sleep.write_text("#!/bin/sh\nprintf '%s' \"$1\" > " + shlex.quote(str(delay)) + "\nexit 17\n")
        sleep.chmod(0o700)
        self.script.write_text(self.script.read_text().replace("/bin/sleep", shlex.quote(str(sleep))))
        for response, code in [('{"ok":true}\n200', 0), ('', 7)]:
            with self.subTest(code=code):
                self.set_response(response, code)
                result = self.run_script(url=URL)
                self.assertEqual(result.returncode, 17)
                self.assertIn("ogni 30s", result.stdout)
                self.assertIn("serious", result.stdout)
                self.assertGreaterEqual(int(delay.read_text()), 27)
                self.assertLessEqual(int(delay.read_text()), 30)

    def test_installer_configuration_every_30s(self):
        installer = (ROOT / "install-temperature-service.sh").read_text()
        xml = installer.split("<<'PLIST_XML'\n", 1)[1].split('\nPLIST_XML', 1)[0]
        actual = plistlib.loads(xml.encode())
        expected = plistlib.loads((ROOT / "ch.nmperformance.fithub.temperature.plist").read_bytes())
        del expected["UserName"]
        self.assertEqual(actual, expected)
        self.assertEqual(actual["StartInterval"], 30)
        self.assertTrue(actual["RunAtLoad"])
        self.assertEqual(actual["ProgramArguments"],
                         ["/bin/bash", "/Users/admin/Documents/Fithub/scripts/monitormetrics/temperature.sh", "--service"])
        self.assertEqual(actual["Label"], "ch.nmperformance.fithub.temperature")
        self.assertNotIn("ram.sh", installer)
        self.assertNotIn("cpu.sh", installer)


if __name__ == "__main__":
    unittest.main()
