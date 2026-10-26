"""SSD: spazio APFS e Push simulati, nessuna modifica ai dischi o servizi."""
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
TOKEN = "TOKEN_SSD_FITTIZIO_TEST"
URL = f"https://kuma.example.test/api/push/{TOKEN}"
GIB = 1073741824


@unittest.skipUnless(sys.platform == "darwin", "Usa bash e plutil di macOS")
class SSDMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.root / "request.json"
        self.diskutil = self.root / "fake-diskutil"
        self.curl = self.root / "fake-curl"
        self.script = self.root / "ssd.sh"
        source = (ROOT / "ssd.sh").read_text()
        source = source.replace("/usr/sbin/diskutil", shlex.quote(str(self.diskutil)))
        source = source.replace("/usr/bin/curl", shlex.quote(str(self.curl)))
        self.script.write_text(source)
        self.info = {
            "FilesystemType": "apfs", "Internal": True, "SolidState": True,
            "MountPoint": "/System/Volumes/Data", "APFSContainerSize": 1000 * GIB,
            "APFSContainerFree": 200 * GIB, "FreeSpace": 0, "VolumeSize": 0,
        }
        self.set_info()
        self.set_response('{"ok":true}\n200')

    def set_info(self, overrides=None, omit=None, code=0):
        info = dict(self.info)
        info.update(overrides or {})
        if omit:
            del info[omit]
        self.command(self.diskutil, plistlib.dumps(info).decode(), code)

    def command(self, path, output, code=0):
        path.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(output) + f"\nexit {code}\n")
        path.chmod(0o700)

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
        env.pop("KUMA_SSD_PUSH_URL", None)
        if url is not None:
            env["KUMA_SSD_PUSH_URL"] = url
        return subprocess.run(["/bin/bash", str(self.script), *args], env=env,
                              text=True, capture_output=True, timeout=5)

    def test_container_space_ignores_zero_volume_space(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SSD 80.00% (800.00/1000.00 GiB) - liberi 200.00 GiB", result.stdout)
        self.assertFalse(self.record.exists())

    def test_thresholds_and_recovery(self):
        for free, status, code, marker in [
            (150 * GIB, "up", 0, None),
            (149 * GIB, "up", 0, "AVVISO"),
            (100 * GIB, "up", 0, "AVVISO"),
            (99 * GIB, "down", 1, "CRITICO"),
            (200 * GIB, "up", 0, None),
        ]:
            with self.subTest(free=free):
                self.set_info({"APFSContainerFree": free})
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(f"status={status}", json.loads(self.record.read_text())["args"])
                if marker:
                    self.assertIn(marker, result.stdout)
                else:
                    self.assertNotIn("AVVISO", result.stdout)
                    self.assertNotIn("CRITICO", result.stdout)

    def test_full_and_empty_container(self):
        for free, percent, code in [(0, "100.00", 1), (1000 * GIB, "0.00", 0)]:
            with self.subTest(free=free):
                self.set_info({"APFSContainerFree": free})
                result = self.run_script("--dry-run")
                self.assertEqual(result.returncode, code)
                self.assertIn(f"SSD {percent}%", result.stdout)

    def test_thresholds_before_rounding(self):
        for free, code, marker in [(150 * GIB - 1, 0, "AVVISO"), (100 * GIB - 1, 1, "CRITICO")]:
            with self.subTest(free=free):
                self.set_info({"APFSContainerFree": free})
                result = self.run_script("--dry-run")
                self.assertEqual(result.returncode, code)
                self.assertIn(marker, result.stdout)

    def test_invalid_or_missing_container_counters_report_down(self):
        for overrides, omit in [
            ({"APFSContainerSize": 0}, None), ({"APFSContainerFree": -1}, None),
            ({"APFSContainerFree": 1001 * GIB}, None),
            ({"APFSContainerSize": "bad"}, None), ({"APFSContainerFree": True}, None),
            ({"FreeSpace": 200 * GIB}, "APFSContainerFree"), ({}, "APFSContainerSize"),
        ]:
            with self.subTest(overrides=overrides, omit=omit):
                self.set_info(overrides, omit=omit)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertIn("SSD: lettura non riuscita", result.stdout)
                self.assertIn("status=down", json.loads(self.record.read_text())["args"])

    def test_wrong_device_or_filesystem_is_rejected(self):
        for overrides in [{"Internal": False}, {"SolidState": False},
                          {"FilesystemType": "hfs"}, {"MountPoint": "/Volumes/External"}]:
            with self.subTest(overrides=overrides):
                self.set_info(overrides)
                self.assertEqual(self.run_script("--dry-run").returncode, 2)

    def test_diskutil_failure_or_invalid_xml(self):
        self.set_info(code=1)
        self.assertEqual(self.run_script("--dry-run").returncode, 2)
        self.command(self.diskutil, "disk unavailable")
        self.assertEqual(self.run_script("--dry-run").returncode, 2)

    def test_push_message_query_replacement_and_token_privacy(self):
        result = self.run_script("--once", url=URL + "?status=down&msg=OLD&ping=99")
        self.assertEqual(result.returncode, 0)
        request = json.loads(self.record.read_text())
        self.assertEqual(request["config"], f'url = "{URL}"\n')
        self.assertIn("--data-urlencode", request["args"])
        self.assertTrue(any(arg.startswith("msg=SSD 80.00% (") for arg in request["args"]))
        self.assertNotIn("ping=", json.dumps(request))
        self.assertNotIn(TOKEN, json.dumps(request["args"]) + result.stdout + result.stderr)

    def test_url_file_environment_override_and_local_only(self):
        file = self.root / "ssd-push-url.txt"
        file.write_bytes((URL + "\r\n").encode())
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.assertEqual(self.run_script("--once", url=URL + "OTHER").returncode, 0)
        self.assertIn(TOKEN + "OTHER", json.loads(self.record.read_text())["config"])
        self.record.unlink()
        self.assertEqual(self.run_script("--dry-run").returncode, 0)
        self.assertFalse(self.record.exists())
        file.write_text("")
        self.assertEqual(self.run_script("--once").returncode, 2)

    def test_rejected_pushes_are_errors_even_when_critical(self):
        self.set_info({"APFSContainerFree": 0})
        for response, code in [('{"ok":false}\n200', 0), ('<html>Login</html>\n200', 0),
                               ('{"ok":true}\n302', 0), ('', 7)]:
            with self.subTest(response=response):
                self.set_response(response, code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_no_url_or_invalid_arguments(self):
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("solo lettura locale", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.run_script("--once", url="bad-url").returncode, 2)
        self.assertEqual(self.run_script("--unknown").returncode, 2)
        self.assertEqual(self.run_script("--once", "--dry-run").returncode, 2)

    def test_continuous_mode_keeps_60s_cadence_after_critical_or_push_error(self):
        self.set_info({"APFSContainerFree": 0})
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
                self.assertEqual(result.returncode, 17)
                self.assertIn("ogni 60s", result.stdout)
                self.assertIn("CRITICO", result.stdout)
                self.assertGreaterEqual(int(delay_file.read_text()), 57)
                self.assertLessEqual(int(delay_file.read_text()), 60)

    def test_installer_configuration_is_dedicated_to_ssd_every_60s(self):
        installer = (ROOT / "install-ssd-service.sh").read_text()
        xml = installer.split("<<'PLIST_XML'\n", 1)[1].split('\nPLIST_XML', 1)[0]
        actual = plistlib.loads(xml.encode())
        expected = plistlib.loads((ROOT / "ch.nmperformance.fithub.ssd.plist").read_bytes())
        del expected["UserName"]
        self.assertEqual(actual, expected)
        self.assertEqual(actual["StartInterval"], 60)
        self.assertEqual(actual["ProgramArguments"],
                         ["/bin/bash", "/Users/admin/Documents/Fithub/scripts/monitormetrics/ssd.sh", "--service"])
        self.assertEqual(actual["Label"], "ch.nmperformance.fithub.ssd")
        self.assertNotIn("cpu.sh", installer)
        self.assertNotIn("ram.sh", installer)


if __name__ == "__main__":
    unittest.main()
