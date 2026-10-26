"""Docker: socket e API simulati, nessun accesso a daemon o rete reali."""
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
TOKEN = "TOKEN_DOCKER_FITTIZIO_TEST"
URL = f"https://kuma.example.test/api/push/{TOKEN}"


@unittest.skipUnless(sys.platform == "darwin", "Usa bash e plutil di macOS")
class DockerMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "account home"
        self.socket = self.home / ".docker/run/docker.sock"
        self.socket.parent.mkdir(parents=True)
        self.socket.touch()
        self.default_socket = self.root / "default.sock"
        self.record = self.root / "push-request.json"
        self.engine_record = self.root / "engine-request.json"
        self.dscl = self.root / "fake-dscl"
        self.curl = self.root / "fake-curl"
        self.script = self.root / "docker.sh"
        self.set_home()
        source = (ROOT / "docker.sh").read_text()
        source = source.replace("/usr/bin/dscl", shlex.quote(str(self.dscl)))
        source = source.replace("/usr/bin/curl", shlex.quote(str(self.curl)))
        source = source.replace("candidate=/var/run/docker.sock", "candidate=" + shlex.quote(str(self.default_socket)))
        # Solo nella copia di test, i marker regolari simulano socket Unix.
        source = source.replace('[[ -S "$candidate" ]]', '[[ -f "$candidate" ]]')
        self.script.write_text(source)
        self.curl.write_text(
            f"#!{sys.executable}\nimport json, pathlib, sys\n"
            f"root = pathlib.Path({str(self.root)!r})\n"
            "kind = 'engine' if '--unix-socket' in sys.argv else 'push'\n"
            "config = '' if kind == 'engine' else sys.stdin.read()\n"
            "(root / (kind + '-request.json')).write_text(json.dumps(\n"
            "    {'config': config, 'args': sys.argv[1:]}))\n"
            "sys.stdout.write((root / (kind + '-body')).read_text())\n"
            "sys.exit(int((root / (kind + '-code')).read_text()))\n"
        )
        self.curl.chmod(0o700)
        self.set_response("engine", "OK\n200")
        self.set_response("push", '{"ok":true}\n200')

    def set_home(self, output=None, code=0):
        output = output if output is not None else f"NFSHomeDirectory: {self.home}\n"
        self.dscl.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(output) + f"\nexit {code}\n")
        self.dscl.chmod(0o700)

    def set_response(self, kind, response, code=0):
        (self.root / (kind + "-body")).write_text(response)
        (self.root / (kind + "-code")).write_text(str(code))

    def run_script(self, *args, url=None):
        env = dict(os.environ)
        env.pop("KUMA_DOCKER_PUSH_URL", None)
        env["DOCKER_HOST"] = "tcp://remote.example.invalid:2375"
        env["DOCKER_CONTEXT"] = "remote-context"
        env["HTTP_PROXY"] = "http://proxy.example.invalid"
        if url is not None:
            env["KUMA_DOCKER_PUSH_URL"] = url
        return subprocess.run(["/bin/bash", str(self.script), *args], env=env,
                              text=True, capture_output=True, timeout=5)

    def push_status(self):
        return next(a for a in json.loads(self.record.read_text())["args"] if a.startswith("status="))

    def test_engine_ping_is_local_bounded_and_ignores_remote_context(self):
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Docker: daemon raggiungibile - API OK", result.stdout)
        self.assertEqual(self.push_status(), "status=up")
        args = json.loads(self.engine_record.read_text())["args"]
        self.assertEqual(args[args.index("--unix-socket") + 1], str(self.socket))
        self.assertEqual(args[args.index("--max-time") + 1], "10")
        self.assertEqual(args[args.index("--noproxy") + 1], "*")
        self.assertIn("http://localhost/_ping", args)
        self.assertNotIn("--data-urlencode", args)
        self.assertNotIn(TOKEN, json.dumps(args))
        self.assertNotIn("--location", args)

    def test_missing_user_socket_falls_back_to_standard_socket(self):
        self.socket.unlink()
        self.default_socket.touch()
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0)
        args = json.loads(self.engine_record.read_text())["args"]
        self.assertEqual(args[args.index("--unix-socket") + 1], str(self.default_socket))

    def test_no_socket_reports_down_without_trying_http(self):
        self.socket.unlink()
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 1)
        self.assertIn("socket locale assente o non accessibile", result.stdout)
        self.assertEqual(self.push_status(), "status=down")
        self.assertFalse(self.engine_record.exists())

    def test_home_lookup_failure_or_invalid_output_uses_only_standard_socket(self):
        self.default_socket.touch()
        for output, code in [("", 1), ("NFSHomeDirectory: relative", 0),
                              ("NFSHomeDirectory: /tmp/a\n/tmp/b", 0)]:
            with self.subTest(output=output, code=code):
                self.set_home(output, code)
                self.assertEqual(self.run_script("--dry-run").returncode, 0)
                args = json.loads(self.engine_record.read_text())["args"]
                self.assertIn(str(self.default_socket), args)

    def test_unreachable_daemon_timeout_or_socket_permission_reports_down(self):
        for code in [7, 28, 22]:
            with self.subTest(code=code):
                self.set_response("engine", "", code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 1)
                self.assertIn("daemon non raggiungibile", result.stdout)
                self.assertEqual(self.push_status(), "status=down")

    def test_unhealthy_user_socket_does_not_switch_to_other_daemon(self):
        self.default_socket.touch()
        self.set_response("engine", "", 7)
        result = self.run_script("--once", url=URL)
        self.assertEqual(result.returncode, 1)
        args = json.loads(self.engine_record.read_text())["args"]
        self.assertIn(str(self.socket), args)
        self.assertNotIn(str(self.default_socket), args)

    def test_unexpected_responses_report_down_and_recovery_reports_up(self):
        for response in ["OK\n302", "OK\n500", "\n200", "<html>Login</html>\n200", "NOT OK\n200"]:
            with self.subTest(response=response):
                self.set_response("engine", response)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.push_status(), "status=down")
        self.set_response("engine", "OK\n200")
        self.assertEqual(self.run_script("--once", url=URL).returncode, 0)
        self.assertEqual(self.push_status(), "status=up")

    def test_dry_run_checks_engine_without_sending_to_kuma(self):
        (self.root / "docker-push-url.txt").write_text(URL)
        self.assertEqual(self.run_script("--dry-run", url=URL).returncode, 0)
        self.assertTrue(self.engine_record.exists())
        self.assertFalse(self.record.exists())

    def test_push_query_replacement_and_token_privacy(self):
        result = self.run_script("--once", url=URL + "?status=down&msg=OLD&ping=99")
        self.assertEqual(result.returncode, 0)
        request = json.loads(self.record.read_text())
        self.assertEqual(request["config"], f'url = "{URL}"\n')
        self.assertIn("msg=Docker: daemon raggiungibile - API OK", request["args"])
        self.assertNotIn("ping=", json.dumps(request))
        self.assertNotIn(TOKEN, json.dumps(request["args"]) + result.stdout + result.stderr)

    def test_url_file_precedence_missing_url_and_invalid_arguments(self):
        file = self.root / "docker-push-url.txt"
        file.write_bytes((URL + "\r\n").encode())
        self.assertEqual(self.run_script("--once").returncode, 0)
        self.assertEqual(self.run_script("--once", url=URL + "OTHER").returncode, 0)
        self.assertIn("OTHER", json.loads(self.record.read_text())["config"])
        file.write_text("")
        self.assertEqual(self.run_script("--once").returncode, 2)
        file.unlink()
        self.record.unlink()
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("solo lettura locale", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.run_script("--once", url="bad").returncode, 2)
        self.assertEqual(self.run_script("--unknown").returncode, 2)
        self.assertEqual(self.run_script("--once", "--dry-run").returncode, 2)

    def test_push_failure_overrides_down_exit_code(self):
        self.set_response("engine", "", 7)
        for response, code in [('{"ok":false}\n200', 0), ('<html>Login</html>\n200', 0),
                               ('{"ok":true}\n302', 0), ('', 7)]:
            with self.subTest(response=response):
                self.set_response("push", response, code)
                result = self.run_script("--once", url=URL)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_continuous_mode_every_30s_survives_daemon_and_kuma_down(self):
        self.set_response("engine", "", 7)
        sleep = self.root / "fake-sleep"
        delay = self.root / "delay"
        sleep.write_text("#!/bin/sh\nprintf '%s' \"$1\" > " + shlex.quote(str(delay)) + "\nexit 17\n")
        sleep.chmod(0o700)
        self.script.write_text(self.script.read_text().replace("/bin/sleep", shlex.quote(str(sleep))))
        for response, code in [('{"ok":true}\n200', 0), ('', 7)]:
            self.set_response("push", response, code)
            result = self.run_script(url=URL)
            self.assertEqual(result.returncode, 17)
            self.assertIn("ogni 30s", result.stdout)
            self.assertIn("daemon non raggiungibile", result.stdout)
            self.assertGreaterEqual(int(delay.read_text()), 27)
            self.assertLessEqual(int(delay.read_text()), 30)

    def test_installer_every_30s_has_dedicated_label_and_files(self):
        installer = (ROOT / "install-docker-service.sh").read_text()
        xml = installer.split("<<'PLIST_XML'\n", 1)[1].split('\nPLIST_XML', 1)[0]
        actual = plistlib.loads(xml.encode())
        expected = plistlib.loads((ROOT / "ch.nmperformance.fithub.docker.plist").read_bytes())
        del expected["UserName"]
        self.assertEqual(actual, expected)
        self.assertEqual(actual["StartInterval"], 30)
        self.assertEqual(actual["Label"], "ch.nmperformance.fithub.docker")
        self.assertEqual(actual["ProgramArguments"],
                         ["/bin/bash", "/Users/admin/Documents/Fithub/scripts/monitormetrics/docker.sh", "--service"])
        self.assertNotIn("temperature.sh", installer)


if __name__ == "__main__":
    unittest.main()
