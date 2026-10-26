"""Log reali di --service nel progetto, con il solo campione hardware simulato."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1] / "scripts/monitormetrics"
METRICS = ["cpu", "ram", "ssd", "swap", "temperature", "docker", "backup", "host"]


@unittest.skipUnless(sys.platform == "darwin", "Usa Bash e lockf di macOS")
class MonitorServiceModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "Fithub"
        self.scripts = self.project / "scripts/monitormetrics"
        self.scripts.mkdir(parents=True)
        self.logs = self.project / "logs/system/monitormetrics"
        self.logs.mkdir(parents=True)
        self.logger = self.project / "fake-logger"
        self.logger.write_text("#!/bin/sh\nexit 0\n")
        self.logger.chmod(0o700)
        for metric in METRICS:
            source = (ROOT / f"{metric}.sh").read_text()
            # Conserva la preparazione reale, inclusi opzioni, log e lock Swap.
            prefix = source.split("sample_and_report() {", 1)[0]
            suffix = "if (( ONCE )); then" + source.split("if (( ONCE )); then", 1)[1]
            sample = '''sample_and_report() {
  printf '%s\\n' 'CAMPIONE_SIMULATO'
  printf '%s\\n' 'ERRORE_SIMULATO' >&2
  return "${FITHUB_SAMPLE_EXIT:-0}"
}

'''
            source = (prefix + sample + suffix).replace("/usr/bin/logger", shlex.quote(str(self.logger)))
            (self.scripts / f"{metric}.sh").write_text(source)

    def run_monitor(self, metric, mode="--service", code=0):
        env = {k: v for k, v in os.environ.items() if not k.startswith("KUMA_")}
        env["FITHUB_SAMPLE_EXIT"] = str(code)
        return subprocess.run(["/bin/bash", str(self.scripts / f"{metric}.sh"), mode],
                              env=env, text=True, capture_output=True, timeout=5)

    def test_all_service_modes_log_in_project_and_preserve_sample_exit_codes(self):
        for metric in METRICS:
            for code in [0, 1, 2]:
                with self.subTest(metric=metric, code=code):
                    result = self.run_monitor(metric, code=code)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")
                    self.assertIn("CAMPIONE_SIMULATO", (self.logs / f"{metric}.log").read_text())
                    self.assertIn("ERRORE_SIMULATO", (self.logs / f"{metric}-error.log").read_text())
                    self.assertEqual((self.logs / f"{metric}.log").stat().st_mode & 0o777, 0o600)
                    self.assertEqual((self.logs / f"{metric}-error.log").stat().st_mode & 0o777, 0o600)

    def test_once_remains_interactive_and_does_not_create_service_logs(self):
        result = self.run_monitor("host", mode="--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("CAMPIONE_SIMULATO", result.stdout)
        self.assertIn("ERRORE_SIMULATO", result.stderr)
        self.assertEqual(list(self.logs.iterdir()), [])

    def test_missing_log_directory_fails_before_sample(self):
        self.logs.rmdir()
        for metric in METRICS:
            with self.subTest(metric=metric):
                result = self.run_monitor(metric)
                self.assertEqual(result.returncode, 78)
                self.assertNotIn("CAMPIONE_SIMULATO", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
