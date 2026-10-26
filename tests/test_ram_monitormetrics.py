"""RAM: contatori VM e Push simulati, nessuna modifica ai servizi del Mac."""
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
TOKEN = "TOKEN_RAM_FITTIZIO_TEST"
URL = f"https://kuma.example.test/api/push/{TOKEN}"


@unittest.skipUnless(sys.platform == "darwin", "Usa bash e plutil di macOS")
class RAMMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.root / "request.json"
        self.sysctl = self.root / "fake-sysctl"
        self.vm_stat = self.root / "fake-vm-stat"
        self.curl = self.root / "fake-curl"
        self.script = self.root / "ram.sh"
        source = (ROOT / "ram.sh").read_text()
        for real, fake in [("/usr/sbin/sysctl", self.sysctl),
                           ("/usr/bin/vm_stat", self.vm_stat),
                           ("/usr/bin/curl", self.curl)]:
            source = source.replace(real, shlex.quote(str(fake)))
        self.script.write_text(source)
        self.set_memory()
        self.set_response('{"ok":true}\n200')

    def command(self, path, output, code=0):
        path.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(output) + f"\nexit {code}\n")
        path.chmod(0o700)

    def set_memory(self, *, page_size=16384, total_pages=1000, anonymous=700,
                   wired=100, compressed=100, purgeable=100, stored=999999):
        self.command(self.sysctl, str(total_pages * page_size))
        self.stats = (
            f"Mach Virtual Memory Statistics: (page size of {page_size} bytes)\n"
            f"Anonymous pages: {anonymous}.\n"
            f"Pages wired down: {wired}.\n"
            f"Pages occupied by compressor: {compressed}.\n"
            f"Pages purgeable: {purgeable}.\n"
            f"Pages stored in compressor: {stored}.\n"
            "File-backed pages: 500000.\nPages inactive: 100000.\n"
        )
        self.command(self.vm_stat, self.stats)

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
        env.pop("KUMA_RAM_PUSH_URL", None)
        if url is not None:
            env["KUMA_RAM_PUSH_URL"] = url
        return subprocess.run(["/bin/bash", str(self.script), *args], env=env,
                              text=True, capture_output=True, timeout=5)

    def test_native_page_sizes_and_physical_compressor(self):
        for page_size in [4096, 16384]:
            with self.subTest(page_size=page_size):
                self.set_memory(page_size=page_size)
                result = self.run_script("--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("RAM 80.00%", result.stdout)
                self.assertFalse(self.record.exists())

    def test_gib_units_and_cache_exclusion(self):
        self.set_memory(total_pages=1048576, anonymous=458752, wired=65536,
                        compressed=131072, purgeable=131072)
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertIn("RAM 50.00% (8.00/16.00 GiB)", result.stdout)

    def test_threshold_and_recovery(self):
        for anonymous, expected_status, expected_code, percent in [
            (800, "up", 0, "90.00"),
            (801, "down", 1, "90.10"),
            (700, "up", 0, "80.00"),
        ]:
            with self.subTest(anonymous=anonymous):
                self.set_memory(anonymous=anonymous)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                request = json.loads(self.record.read_text())
                self.assertIn(f"status={expected_status}", request["args"])
                self.assertIn(f"RAM {percent}%", result.stdout)
                self.assertEqual("CRITICO" in result.stdout, expected_status == "down")

    def test_threshold_before_rounding(self):
        self.set_memory(total_pages=1000000, anonymous=800001,
                        wired=100000, compressed=100000, purgeable=100000)
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=down", json.loads(self.record.read_text())["args"])

    def test_invalid_vm_readings_report_down(self):
        for stats in ["", self.stats.replace("16384", "invalid"),
                      self.stats.replace("Pages purgeable: 100.\n", ""),
                      self.stats.replace("Anonymous pages: 700.", "Anonymous pages: -1."),
                      self.stats.replace("Anonymous pages: 700.", "Anonymous pages: 2000."),
                      self.stats + "Pages wired down: 100.\n",
                      self.stats.replace("Pages purgeable: 100.", "Pages purgeable: 701.")]:
            with self.subTest(stats=stats):
                self.command(self.vm_stat, stats)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertIn("RAM: lettura non riuscita", result.stdout)
                self.assertIn("status=down", json.loads(self.record.read_text())["args"])

    def test_invalid_total_memory_and_command_errors(self):
        for output, code in [("0", 0), ("bad", 0), ("", 1), ("16384000", 1)]:
            with self.subTest(output=output, code=code):
                self.command(self.sysctl, output, code)
                self.assertEqual(self.run_script("--once", url=URL).returncode, 2)
        self.set_memory()
        self.command(self.vm_stat, self.stats, code=1)
        self.assertEqual(self.run_script("--once", url=URL).returncode, 2)

    def test_push_message_query_replacement_and_token_privacy(self):
        result = self.run_script("--once", url=URL + "?status=down&msg=OLD&ping=99")
        self.assertEqual(result.returncode, 0)
        request = json.loads(self.record.read_text())
        self.assertEqual(request["config"], f'url = "{URL}"\n')
        self.assertIn("--data-urlencode", request["args"])
        self.assertTrue(any(arg.startswith("msg=RAM 80.00% (") for arg in request["args"]))
        self.assertNotIn("ping=", json.dumps(request))
        self.assertNotIn(TOKEN, json.dumps(request["args"]) + result.stdout + result.stderr)

    def test_url_file_environment_override_and_local_only(self):
        file = self.root / "ram-push-url.txt"
        file.write_bytes((URL + "\r\n").encode())
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.assertEqual(self.run_script("--once", url=URL + "OTHER").returncode, 0)
        self.assertIn(TOKEN + "OTHER", json.loads(self.record.read_text())["config"])
        self.record.unlink()
        self.assertEqual(self.run_script("--dry-run").returncode, 0)
        self.assertFalse(self.record.exists())
        file.write_text("")
        self.assertEqual(self.run_script("--once").returncode, 2)

    def test_missing_url_warns_without_sending(self):
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("solo lettura locale", result.stderr)
        self.assertFalse(self.record.exists())

    def test_rejected_pushes_are_errors_even_when_ram_is_high(self):
        self.set_memory(anonymous=801)
        for response, code in [('{"ok":false}\n200', 0),
                               ('<html>Login</html>\n200', 0),
                               ('{"ok":true}\n302', 0), ('', 7)]:
            with self.subTest(response=response):
                self.set_response(response, code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_invalid_url_and_arguments(self):
        result = self.run_script("--once", url='https://bad"host/api/push/SECRET')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("SECRET", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.run_script("--unknown").returncode, 2)
        self.assertEqual(self.run_script("--once", "--dry-run").returncode, 2)

    def test_continuous_mode_survives_critical_ram_and_failed_push(self):
        self.set_memory(anonymous=801)
        sleep = self.root / "fake-sleep"
        delay_file = self.root / "delay"
        sleep.write_text(
            "#!/bin/sh\nprintf '%s' \"$1\" > " + shlex.quote(str(delay_file)) + "\nexit 17\n"
        )
        sleep.chmod(0o700)
        self.script.write_text(self.script.read_text().replace("/bin/sleep", shlex.quote(str(sleep))))
        for response, code in [('{"ok":true}\n200', 0), ('', 7)]:
            with self.subTest(code=code):
                self.set_response(response, code)
                result = self.run_script(url=URL)
                # Arriva all'attesa successiva anche dopo DOWN o errore di rete.
                self.assertEqual(result.returncode, 17)
                self.assertIn("ogni 15s", result.stdout)
                self.assertIn("CRITICO", result.stdout)
                self.assertGreaterEqual(int(delay_file.read_text()), 12)
                self.assertLessEqual(int(delay_file.read_text()), 15)

    def test_installer_configuration_is_dedicated_to_ram(self):
        installer = (ROOT / "install-ram-service.sh").read_text()
        xml = installer.split("<<'PLIST_XML'\n", 1)[1].split('\nPLIST_XML', 1)[0]
        actual = plistlib.loads(xml.encode())
        expected = plistlib.loads((ROOT / "ch.nmperformance.fithub.ram.plist").read_bytes())
        del expected["UserName"]
        self.assertEqual(actual, expected)
        self.assertEqual(actual["StartInterval"], 15)
        self.assertEqual(actual["ProgramArguments"][-1], "--service")
        self.assertEqual(actual["Label"], "ch.nmperformance.fithub.ram")
        self.assertNotIn("cpu", installer)


if __name__ == "__main__":
    unittest.main()
