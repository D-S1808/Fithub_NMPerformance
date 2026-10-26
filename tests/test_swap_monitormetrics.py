"""Swap: campioni, tempo e Kuma simulati; nessuna modifica alla memoria virtuale."""
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
URL = "https://kuma.example.test/api/push/TOKEN_SWAP_FITTIZIO_TEST"
BASE = 1793000000
BOOT = BASE - 10000


@unittest.skipUnless(sys.platform == "darwin", "Usa i comandi macOS")
class SwapMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "swap.sh"
        self.record = self.root / "request.json"
        self.state = self.root / "swap-state" / "history"
        self.usage = self.root / "usage"
        self.boot = self.root / "boot"
        self.now = self.root / "now"
        self.sysctl = self.root / "fake-sysctl"
        self.date = self.root / "fake-date"
        self.curl = self.root / "fake-curl"
        self.sysctl.write_text(
            f"#!{sys.executable}\nimport pathlib, sys\n"
            f"root = pathlib.Path({str(self.root)!r})\n"
            "key = {'vm.swapusage': 'usage', 'kern.boottime': 'boot'}[sys.argv[2]]\n"
            "if (root / (key + '-error')).exists(): sys.exit(1)\n"
            "print((root / key).read_text())\n"
        )
        self.date.write_text(
            "#!/bin/sh\nif [ \"$1\" = '+%s' ]; then /bin/cat " + shlex.quote(str(self.now)) +
            "; else printf 'TEST_TIME'; fi\n"
        )
        self.sysctl.chmod(0o700)
        self.date.chmod(0o700)
        source = (ROOT / "swap.sh").read_text()
        for real, fake in [("/usr/sbin/sysctl", self.sysctl), ("/bin/date", self.date),
                           ("/usr/bin/curl", self.curl)]:
            source = source.replace(real, shlex.quote(str(fake)))
        self.script.write_text(source)
        self.set_usage()
        self.set_boot(BOOT)
        self.set_time(0)
        self.set_response('{"ok":true}\n200')

    def set_time(self, seconds):
        self.now.write_text(str(BASE + seconds))

    def set_boot(self, boot):
        self.boot.write_text(f"{{ sec = {boot}, usec = 123 }} example date")

    def set_usage(self, used="512.00M", total="4096.00M", free="3584.00M"):
        self.usage.write_text(f"total = {total} used = {used} free = {free} (encrypted)")

    def high(self):
        self.set_usage(used="3072.00M", free="1024.00M")

    def set_response(self, response, code=0):
        self.curl.write_text(
            f"#!{sys.executable}\nimport json, pathlib, sys\n"
            f"pathlib.Path({str(self.record)!r}).write_text(json.dumps(\n"
            "    {'config': sys.stdin.read(), 'args': sys.argv[1:]}))\n"
            f"sys.stdout.write({response!r})\nsys.exit({code})\n"
        )
        self.curl.chmod(0o700)

    def run_script(self, *args, url=URL):
        env = dict(os.environ)
        env.pop("KUMA_SWAP_PUSH_URL", None)
        if url is not None:
            env["KUMA_SWAP_PUSH_URL"] = url
        return subprocess.run(["/bin/bash", str(self.script), *args], env=env,
                              text=True, capture_output=True, timeout=5)

    def request_status(self):
        return next(a for a in json.loads(self.record.read_text())["args"] if a.startswith("status="))

    def test_native_format_and_binary_units(self):
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SWAP 12.50% (0.50/4.00 GiB) - liberi 3.50 GiB", result.stdout)
        self.assertEqual(self.request_status(), "status=up")
        self.set_usage(used="0.50G", total="4.00G", free="3.50G")
        self.assertEqual(self.run_script("--once").returncode, 0)

    def test_warning_boundaries_and_high_sample_stays_up(self):
        for used, free, warning in [("1024.00M", "3072.00M", False),
                                    ("1024.01M", "3071.99M", True),
                                    ("2048.00M", "2048.00M", True),
                                    ("2048.01M", "2047.99M", True)]:
            with self.subTest(used=used):
                self.set_usage(used, free=free)
                result = self.run_script("--once")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("AVVISO" in result.stdout, warning)
                self.assertEqual(self.request_status(), "status=up")

    def test_persistent_five_minutes_then_recovery(self):
        self.high()
        for seconds in range(0, 301, 30):
            self.set_time(seconds)
            result = self.run_script("--once")
            self.assertEqual(result.returncode, int(seconds == 300), result.stderr)
            self.assertEqual(self.request_status(), "status=down" if seconds == 300 else "status=up")
        self.assertIn("CRITICO", result.stdout)
        self.assertIn("300s", result.stdout)
        self.set_time(330)
        self.set_usage(used="2048.00M", free="2048.00M")
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.set_time(360)
        self.high()
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("0/300s", result.stdout)

    def test_reboot_gap_and_backward_clock_restart_counting(self):
        for condition in ["reboot", "gap", "clock"]:
            with self.subTest(condition=condition):
                self.high()
                self.state.parent.mkdir(exist_ok=True)
                self.state.write_text(f"1 {BOOT} {BASE - 300} {BASE}\n")
                self.set_time(30)
                self.set_boot(BOOT)
                if condition == "reboot": self.set_boot(BASE + 10)
                if condition == "gap": self.set_time(76)
                if condition == "clock": self.set_time(-1)
                result = self.run_script("--once")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("0/300s", result.stdout)

    def test_malformed_or_missing_history_does_not_invent_duration(self):
        self.high()
        self.state.parent.mkdir()
        for history in ["", "bad\n", f"1 {BOOT} 1+2 {BASE}\n", f"1 {BOOT} {BASE} {BASE} extra\n"]:
            with self.subTest(history=history):
                self.state.write_text(history)
                result = self.run_script("--once")
                self.assertEqual(result.returncode, 0)
                self.assertIn("0/300s", result.stdout)

    def test_failed_read_interrupts_duration(self):
        self.high()
        self.state.parent.mkdir()
        self.state.write_text(f"1 {BOOT} {BASE - 300} {BASE}\n")
        self.set_time(30)
        self.usage.write_text("unavailable")
        self.assertEqual(self.run_script("--once").returncode, 2)
        self.assertEqual(self.request_status(), "status=down")
        self.high()
        self.set_time(60)
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("0/300s", result.stdout)

    def test_zero_swap_and_invalid_or_inconsistent_data(self):
        self.set_usage(used="0.00M", total="0.00M", free="0.00M")
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("SWAP 0.00%", result.stdout)
        for output in ["", "total = 10.00M used = -1.00M free = 11.00M",
                       "total = 10.00M used = 12.00M free = 0.00M",
                       "total = 10.00M used = 1.00M free = 8.00M",
                       "total = 10.00M used = 1.00Q free = 9.00M",
                       "total = 10.00M used = 1.00M used = 1.00M free = 9.00M"]:
            with self.subTest(output=output):
                self.usage.write_text(output)
                self.assertEqual(self.run_script("--once").returncode, 2)

    def test_sysctl_or_boot_failure(self):
        for failure in ["usage-error", "boot-error"]:
            file = self.root / failure
            file.touch()
            self.assertEqual(self.run_script("--once").returncode, 2)
            file.unlink()
        self.boot.write_text("{ usec = 123 }")
        self.assertEqual(self.run_script("--once").returncode, 2)

    def test_dry_run_never_creates_or_updates_state(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.state.parent.exists())
        self.assertFalse(self.record.exists())
        self.high()
        self.state.parent.mkdir()
        history = f"1 {BOOT} {BASE - 300} {BASE}\n"
        self.state.write_text(history)
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state.read_text(), history)
        self.assertFalse(self.record.exists())

    def test_history_save_failure_reports_down(self):
        self.state.parent.mkdir()
        self.state.mkdir()
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cronologia Swap non salvabile", result.stdout)
        self.assertEqual(self.request_status(), "status=down")

    def test_concurrent_writer_is_rejected_and_lock_releases(self):
        self.assertEqual(self.run_script("--once").returncode, 0)
        lock = str(self.state.parent / ".lock")
        holder = subprocess.Popen(
            ["/usr/bin/lockf", "-k", "-t", "0", lock, "/bin/sh", "-c", "echo READY; cat >/dev/null"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(holder.stdout.readline(), b"READY\n")
            result = self.run_script("--once")
            self.assertEqual(result.returncode, 2)
            self.assertIn("altro monitor", result.stderr)
        finally:
            holder.communicate(timeout=5)
        self.assertEqual(self.run_script("--once").returncode, 0)

    def test_push_query_token_privacy_and_network_failure_preserve_history(self):
        self.high()
        result = self.run_script("--once", url=URL + "?status=down&msg=OLD&ping=99")
        self.assertEqual(result.returncode, 0)
        request = json.loads(self.record.read_text())
        self.assertEqual(request["config"], f'url = "{URL}"\n')
        self.assertTrue(any(a.startswith("msg=SWAP ") for a in request["args"]))
        self.assertNotIn("ping=", json.dumps(request))
        self.assertNotIn("TOKEN_SWAP", json.dumps(request["args"]) + result.stdout + result.stderr)
        for response, code in [('{"ok":false}\n200', 0), ('<html>Login</html>\n200', 0),
                               ('{"ok":true}\n302', 0), ('', 7)]:
            self.set_response(response, code)
            self.set_time(30)
            self.assertEqual(self.run_script("--once").returncode, 2)
            self.assertEqual(int(self.state.read_text().split()[2]), BASE)

    def test_url_file_environment_and_missing_url(self):
        file = self.root / "swap-push-url.txt"
        file.write_bytes((URL + "\r\n").encode())
        self.assertEqual(self.run_script("--once", url=None).returncode, 0)
        self.assertEqual(self.run_script("--once", url=URL + "OTHER").returncode, 0)
        self.assertIn("OTHER", json.loads(self.record.read_text())["config"])
        file.unlink()
        self.record.unlink()
        result = self.run_script("--once", url=None)
        self.assertEqual(result.returncode, 0)
        self.assertIn("solo lettura locale", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.run_script("--unknown").returncode, 2)
        self.assertEqual(self.run_script("--once", url="bad").returncode, 2)

    def test_continuous_mode_uses_30s_and_survives_critical(self):
        self.high()
        self.state.parent.mkdir()
        self.state.write_text(f"1 {BOOT} {BASE - 300} {BASE}\n")
        sleep = self.root / "fake-sleep"
        delay = self.root / "delay"
        sleep.write_text("#!/bin/sh\nprintf '%s' \"$1\" > " + shlex.quote(str(delay)) + "\nexit 17\n")
        sleep.chmod(0o700)
        self.script.write_text(self.script.read_text().replace("/bin/sleep", shlex.quote(str(sleep))))
        result = self.run_script()
        self.assertEqual(result.returncode, 17)
        self.assertIn("CRITICO", result.stdout)
        self.assertIn("ogni 30s", result.stdout)
        self.assertGreaterEqual(int(delay.read_text()), 27)
        self.assertLessEqual(int(delay.read_text()), 30)

    def test_installer_every_30s_and_private_state(self):
        installer = (ROOT / "install-swap-service.sh").read_text()
        xml = installer.split("<<'PLIST_XML'\n", 1)[1].split('\nPLIST_XML', 1)[0]
        actual = plistlib.loads(xml.encode())
        expected = plistlib.loads((ROOT / "ch.nmperformance.fithub.swap.plist").read_bytes())
        del expected["UserName"]
        self.assertEqual(actual, expected)
        self.assertEqual(actual["StartInterval"], 30)
        self.assertEqual(actual["ProgramArguments"][-1], "--service")
        self.assertEqual(actual["Label"], "ch.nmperformance.fithub.swap")
        self.assertIn('STATE_DIR="$SCRIPT_DIR/swap-state"', installer)
        self.assertIn('-m 700 "$STATE_DIR"', installer)


if __name__ == "__main__":
    unittest.main()
