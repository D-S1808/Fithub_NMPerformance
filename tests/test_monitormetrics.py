"""Prove isolate: nessuna lettura CPU reale e nessuna richiesta di rete."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/monitormetrics/cpu.sh"
TOKEN = "TOKEN_FITTIZIO_TEST"
URL = f"https://kuma.example.test/api/push/{TOKEN}"
SAMPLES = """CPU usage: 75.0% user, 15.0% sys, 10.0% idle
CPU usage: 1.0% user, 0.75% sys, 98.25% idle
"""


@unittest.skipUnless(sys.platform == "darwin", "Usa bash e plutil di macOS")
class CPUMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.root / "request.json"
        self.top = self.root / "fake-top"
        self.curl = self.root / "fake-curl"
        self.script = self.root / "cpu.sh"
        source = SCRIPT.read_text()
        # Le sostituzioni riguardano solo la copia temporanea sotto test.
        source = source.replace("/usr/bin/top", shlex.quote(str(self.top)))
        source = source.replace("/usr/bin/curl", shlex.quote(str(self.curl)))
        self.script.write_text(source)
        self.set_top(SAMPLES)
        self.set_response('{"ok":true}\n200')

    def set_top(self, output, code=0):
        self.top.write_text(
            "#!/bin/sh\nprintf '%s' " + shlex.quote(output) + f"\nexit {code}\n"
        )
        self.top.chmod(0o700)

    def set_response(self, response, code=0):
        self.curl.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(self.record)!r}).write_text(json.dumps(\n"
            "    {'config': sys.stdin.read(), 'args': sys.argv[1:]}))\n"
            f"sys.stdout.write({response!r})\n"
            f"sys.exit({code})\n"
        )
        self.curl.chmod(0o700)

    def run_script(self, *args, url=None):
        env = dict(os.environ)
        env.pop("KUMA_CPU_PUSH_URL", None)
        if url is not None:
            env["KUMA_CPU_PUSH_URL"] = url
        return subprocess.run(
            ["/bin/bash", str(self.script), *args], env=env,
            text=True, capture_output=True, timeout=5,
        )

    def test_second_sample_and_normalized_percent(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CPU 1.75%", result.stdout)
        self.assertNotIn("CPU 90.00%", result.stdout)
        self.assertFalse(self.record.exists())

    def test_push_replaces_query_encodes_message_and_hides_token(self):
        result = self.run_script("--once", url=URL + "?status=down&msg=OLD&ping=99")
        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads(self.record.read_text())
        self.assertEqual(request["config"], f'url = "{URL}"\n')
        self.assertIn("msg=CPU 1.75%", request["args"])
        self.assertIn("status=up", request["args"])
        self.assertIn("--data-urlencode", request["args"])
        self.assertNotIn(TOKEN, json.dumps(request["args"]))
        self.assertNotIn("ping=", json.dumps(request))
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_url_file_and_dry_run_ignores_url(self):
        (self.root / "cpu-push-url.txt").write_text(URL + "\n")
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.record.unlink()
        self.assertEqual(self.run_script("--dry-run").returncode, 0)
        self.assertFalse(self.record.exists())

    def test_invalid_sample_reports_down(self):
        for samples in ["", SAMPLES.splitlines()[0], SAMPLES.replace("98.25", "101")]:
            with self.subTest(samples=samples):
                self.set_top(samples)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                request = json.loads(self.record.read_text())
                self.assertIn("status=down", request["args"])
                self.assertIn("lettura non riuscita", result.stdout)

    def test_top_failure_reports_down(self):
        self.set_top(SAMPLES, code=1)
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 2)
        self.assertIn("status=down", json.loads(self.record.read_text())["args"])

    def test_rejected_or_invalid_kuma_responses_fail(self):
        for response, code in [
            ('{"ok":false}\n200', 0),
            ('<html>Login</html>\n200', 0),
            ('{"ok":true}\n302', 0),
            ('{"ok":true}\n404', 0),
            ('\n200', 0),
            ('', 7),
        ]:
            with self.subTest(response=response, code=code):
                self.set_response(response, code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertIn("invio a Uptime Kuma fallito", result.stderr)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_bad_url_is_rejected_without_request(self):
        result = self.run_script("--once", url='https://bad"host/api/push/SECRET')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.record.exists())
        self.assertNotIn("SECRET", result.stderr)

    def test_local_once_and_usage_errors(self):
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.assertFalse(self.record.exists())
        for args in [("--invalid",), ("--once", "--dry-run")]:
            with self.subTest(args=args):
                self.assertEqual(self.run_script(*args).returncode, 2)

    def test_cadence_includes_time_spent_sampling(self):
        # Simula due secondi di raccolta e interrompe il ciclo alla prima attesa.
        self.top.write_text("#!/bin/sh\n/bin/sleep 2\nprintf '%s' " + shlex.quote(SAMPLES))
        sleep = self.root / "fake-sleep"
        delay_file = self.root / "delay"
        sleep.write_text(
            "#!/bin/sh\nprintf '%s' \"$1\" > " + shlex.quote(str(delay_file)) + "\nexit 17\n"
        )
        sleep.chmod(0o700)
        self.script.write_text(
            self.script.read_text().replace("/bin/sleep", shlex.quote(str(sleep)))
        )
        result = self.run_script()
        self.assertEqual(result.returncode, 17)
        self.assertIn("ogni 15s", result.stdout)
        self.assertIn("CPU 1.75%", result.stdout)
        self.assertGreaterEqual(int(delay_file.read_text()), 11)
        self.assertLessEqual(int(delay_file.read_text()), 13)


if __name__ == "__main__":
    unittest.main()
