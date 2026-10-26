"""Update metadata and failure paths, without contacting registries or installing updates."""
import copy
from contextlib import redirect_stdout
import datetime as dt
import fcntl
import importlib.util
import io
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
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "scripts/updatecheck"
spec = importlib.util.spec_from_file_location("update_checker", SOURCE / "update_checker.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
D1, D2, LEAF = ("sha256:" + c * 64 for c in "123")


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.scripts = self.root / "scripts/updatecheck"
        self.scripts.mkdir(parents=True)
        shutil.copyfile(SOURCE / "updatecheck.example.json", self.scripts / "updatecheck.example.json")
        self.cfg = m.configured(self.scripts)

    def test_versions_and_revisions(self):
        for old, new, state in [("17.2", "17.3", "UPDATE"), ("17.2", "18.0", "MAJOR"),
                                ("17.3", "17.2", "OK"), ("3.2_1", "3.2_2", "UPDATE"),
                                ("v2.1.0", "2.1", "OK"), ("2025.1", "2026.1", "UPDATE")]:
            with self.subTest(old=old, new=new):
                self.assertEqual(m.change_kind(old, new), state)

    def test_image_references_with_ports_and_digests(self):
        self.assertEqual(m.image_ref("registry.test:5000/org/app:1.2"), ("registry.test:5000/org/app", "1.2", ""))
        self.assertEqual(m.image_ref("postgres@" + D1), ("postgres", "latest", D1))
        for ref in ["https://foo/image", "-foo", "postgres@sha256:abc", "x\nsecret"]:
            with self.assertRaises(m.CheckError):
                m.image_ref(ref)

    def test_config_monday_19_and_validation(self):
        self.assertEqual(self.cfg["schedule"], dict(weekday=1, hour=19, minute=0))
        for value in [24, True, "19"]:
            cfg = copy.deepcopy(self.cfg)
            cfg["schedule"]["hour"] = value
            (self.scripts / "updatecheck.json").write_text(json.dumps(cfg))
            with self.assertRaises(m.CheckError):
                m.configured(self.scripts)

    def test_runtime_requires_stable_supported_backup_branch(self):
        class Version(tuple):
            releaselevel = "final"
        for version, valid in [((3, 13, 9), False), ((3, 14, 8), False), ((3, 14, 9), True), ((3, 15, 0), False)]:
            with patch.object(m.sys, "version_info", Version(version)):
                if valid:
                    m.runtime_check()
                else:
                    with self.assertRaises(m.CheckError):
                        m.runtime_check()

    def test_no_checks_is_error_and_updates_down_is_configurable(self):
        self.assertEqual(m.summary([m.row("x", "x", "SKIP")])["status"], "down")
        items = [m.row("x", "x", "MAJOR")]
        self.assertEqual(m.summary(items)["exit_code"], 1)
        self.assertEqual(m.summary(items)["status"], "down")
        self.assertEqual(m.summary(items, False)["status"], "up")
        self.assertEqual(m.summary([m.row("x", "x", "ERROR")], False)["status"], "down")

    def test_independent_checks_continue_after_error(self):
        with patch.object(m, "docker_rows", side_effect=m.CheckError("Registry non raggiungibile")), \
                patch.object(m, "desktop_rows", return_value=[m.row("Host", "desktop", "OK")]), \
                patch.object(m, "macos_rows", return_value=[m.row("Host", "macOS", "UPDATE")]), \
                patch.object(m, "brew_rows", return_value=[]), patch.object(m, "application_rows", return_value=[]), \
                patch.object(m, "git_rows", return_value=[]):
            rows = m.collect(self.cfg, self.root)
        self.assertEqual([r["status"] for r in rows], ["ERROR", "OK", "UPDATE"])

    def docker(self, *, ref="postgres:17", version="17.2", versions=None, env_key="PG_VERSION",
               package=None, malformed=False, ids=None, state="running"):
        calls = []
        cfg = copy.deepcopy(self.cfg["docker"])
        def invoke(args, **kw):
            calls.append(args)
            if "container" in args and "ls" in args:
                return ids or "a" * 12 + "\n", ""
            if "container" in args:
                obj = dict(name="/service-" + args[-1], image=ref, id=D1, state=state)
            elif "exec" in args:
                obj = package or {}
            elif "image" in args:
                obj = [] if malformed else dict(Config=dict(Labels=None, Env=[env_key + "=" + version, "PRIVATE_PASSWORD=secret"]))
            else:
                self.fail("Unexpected Docker command: " + repr(args))
            return json.dumps(obj), ""
        with patch.object(m, "tool", return_value="/fake/docker"), patch.object(m, "command", side_effect=invoke), \
                patch.object(m, "service_versions", return_value=versions or ["17.2"]) as catalog:
            rows = m.docker_rows(cfg)
        return rows, calls, catalog

    def test_docker_checks_local_versions_without_registry_or_login(self):
        rows, calls, _ = self.docker(versions=["17.3", "18.0"])
        self.assertEqual(rows[0]["status"], "UPDATE")
        self.assertEqual(rows[0]["available"], "17.3")
        self.assertIn("ricostruzioni Docker non verificate", rows[0]["detail"])
        self.assertIn("--all", calls[0])
        self.assertFalse(any(set(c) & {"pull", "login", "buildx", "manifest", "run", "restart", "prune"} for c in calls))
        self.assertNotIn("PRIVATE_PASSWORD", json.dumps(rows))
        self.assertEqual(self.docker()[0][0]["status"], "OK")

    def test_service_comparison_stays_on_installed_branch(self):
        for ref, installed, key, versions, expected in [
                ("postgres:latest", "17.2", "PG_VERSION", ["17.3", "18.1"], "17.3"),
                ("redis:7-alpine", "7.4.10", "REDIS_VERSION", ["7.4.11", "8.0.0"], "7.4.11"),
                ("mysql:26.10", "26.10.0-1.el9", "MYSQL_VERSION", ["26.10.1", "9.7.4"], "26.10.1"),
                ("node:22-alpine", "22.23.3", "NODE_VERSION", ["22.23.4", "26.12.1"], "22.23.4")]:
            with self.subTest(ref=ref):
                result = self.docker(ref=ref, version=installed, env_key=key, versions=versions)[0][0]
                self.assertEqual(result["available"], expected)
                self.assertEqual(result["status"], "UPDATE")

    def test_missing_branch_requires_review_not_false_ok(self):
        rows, _, _ = self.docker(versions=["18.1"])
        self.assertEqual(rows[0]["status"], "REVIEW")
        rows, _, _ = self.docker(version="20.1", versions=["18.1"])
        self.assertEqual(rows[0]["status"], "SKIP")

    def test_versions_belong_to_application_not_base_runtime(self):
        config = dict(Env=["PG_VERSION=17.11", "NODE_VERSION=22.23.4", "MYSQL_VERSION=26.10.0-1.el9"])
        for ref, version in [("louislam/uptime-kuma:2", ""), ("node:22-alpine", "22.23.4"),
                             ("docker.io/library/mysql:26.10", "26.10.0"), ("postgres:17", "17.11"),
                             ("private.test/library/postgres:17", ""), ("org/app:2", "")]:
            with self.subTest(ref=ref):
                self.assertEqual(m.image_version(config, ref), version)

    def test_kuma_reads_its_package_not_node_version(self):
        rows, calls, _ = self.docker(ref="louislam/uptime-kuma:2", env_key="NODE_VERSION", version="22.23.4",
                                     package=dict(name="uptime-kuma", version="2.5.6"), versions=["2.5.7"])
        self.assertEqual(rows[0]["installed"], "2.5.6")
        self.assertEqual(rows[0]["available"], "2.5.7")
        self.assertEqual(rows[0]["status"], "UPDATE")
        self.assertNotIn("22.23.4", json.dumps(rows))
        self.assertTrue(any(c[-2:] == ["/bin/cat", "/app/package.json"] for c in calls))

    def test_stopped_unknown_and_local_containers_are_censused(self):
        rows, calls, catalog = self.docker(ref="louislam/uptime-kuma:2", env_key="NODE_VERSION", state="exited")
        self.assertEqual(rows[0]["status"], "SKIP")
        self.assertIn("exited", rows[0]["detail"])
        self.assertFalse(any("exec" in c for c in calls))
        catalog.assert_not_called()
        for ref in ("org/custom:2", "fithub-backend:local", "private.test/postgres:17"):
            with self.subTest(ref=ref):
                rows, _, catalog = self.docker(ref=ref)
                self.assertEqual(rows[0]["status"], "SKIP")
                catalog.assert_not_called()

    def test_public_catalog_is_read_once_for_duplicate_services(self):
        rows, _, catalog = self.docker(ids="a" * 12 + "\n" + "b" * 12)
        self.assertEqual(len(rows), 2)
        catalog.assert_called_once_with("postgres")

    def test_public_catalog_failures_stay_errors_and_are_not_retried(self):
        with patch.object(m, "tool", return_value="/fake/docker"), \
                patch.object(m, "command", return_value=("a" * 12 + "\n" + "b" * 12, "")), \
                patch.object(m, "command_json", side_effect=[
                    dict(name="/postgres1", image="postgres:17", id=D1), dict(Config=dict(Env=["PG_VERSION=17.2"])),
                    dict(name="/postgres2", image="postgres:17", id=D1), dict(Config=dict(Env=["PG_VERSION=17.2"]))]), \
                patch.object(m, "service_versions", side_effect=m.CheckError("Catalogo non raggiungibile")) as catalog:
            rows = m.docker_rows(self.cfg["docker"])
        self.assertEqual([r["status"] for r in rows], ["ERROR", "ERROR"])
        catalog.assert_called_once()
        self.assertEqual(m.summary(rows)["exit_code"], 2)

    def test_public_release_catalogs_filter_prereleases_without_authentication(self):
        with patch.object(m, "get_json", return_value={"17": {"version": "17.11"}, "19": {"version": "19rc1"}}) as get:
            self.assertEqual(m.service_versions("postgres"), ["17.11"])
            self.assertEqual(get.call_args.args[0], "https://raw.githubusercontent.com/docker-library/postgres/master/versions.json")
        with patch.object(m, "get_json", return_value=[dict(version="v22.23.4"), dict(version="v27.0.0-rc.1")]):
            self.assertEqual(m.service_versions("node"), ["22.23.4"])
        releases = [dict(tag_name="v2.5.7", draft=False, prerelease=False),
                    dict(tag_name="2.6.0", draft=False, prerelease=True),
                    dict(tag_name="2.7.0", draft=True, prerelease=False), dict(tag_name="v2.8.0")]
        with patch.object(m, "get_json", return_value=releases) as get:
            self.assertEqual(m.service_versions("louislam/uptime-kuma"), ["2.5.7"])
            self.assertTrue(get.call_args.args[0].startswith("https://api.github.com/repos/louislam/uptime-kuma/releases?"))
        with patch.object(m, "get_json") as get:
            self.assertEqual(m.service_versions("private.test/custom"), [])
            get.assert_not_called()
        for repository in ("postgres", "node", "louislam/uptime-kuma"):
            with patch.object(m, "get_json", return_value="bad"), self.assertRaises(m.CheckError):
                m.service_versions(repository)

    def test_missing_docker_is_skipped_but_unreachable_daemon_is_error(self):
        with patch.object(m, "tool", return_value=None):
            self.assertEqual(m.docker_rows(self.cfg["docker"])[0]["status"], "SKIP")
        with patch.object(m, "tool", return_value="/fake/docker"), \
                patch.object(m, "command", side_effect=m.CheckError("daemon non raggiungibile")):
            with self.assertRaises(m.CheckError):
                m.docker_rows(self.cfg["docker"])
        self.assertEqual(self.docker(malformed=True)[0][0]["status"], "ERROR")

    def test_softwareupdate_lists_updates_without_installing(self):
        calls = []
        def invoke(args, **kw):
            calls.append(args)
            return ("26.0\n", "") if "sw_vers" in args[0] else (" * Label: macOS Tahoe-27.0-ABC\n * Label: Safari27.1-ABC\n", "")
        with patch.object(m, "command", side_effect=invoke):
            rows = m.macos_rows()
        self.assertEqual([r["status"] for r in rows], ["MAJOR", "UPDATE"])
        self.assertEqual(calls[-1], ["/usr/sbin/softwareupdate", "--list"])

    def test_unknown_softwareupdate_output_fails_closed(self):
        with patch.object(m, "command", side_effect=[("26.0", ""), ("network issue", "")]):
            with self.assertRaises(m.CheckError):
                m.macos_rows()
        with patch.object(m, "command", side_effect=[("26.0", ""), ("", "No new software available.")]):
            self.assertEqual(m.macos_rows()[0]["status"], "OK")

    def test_desktop_stable_feed_and_catalog_fallback(self):
        feed = b'<rss xmlns:s="http://www.andymatuschak.org/xml-namespaces/sparkle"><enclosure s:shortVersionString="4.60.0" /></rss>'
        with patch.object(m.Path, "is_file", return_value=True), \
                patch.object(m.Path, "read_bytes", return_value=plistlib.dumps(dict(CFBundleShortVersionString="4.59.0"))), \
                patch.object(m, "get_bytes", return_value=feed):
            self.assertEqual(m.desktop_rows()[0]["available"], "4.60.0")
        with patch.object(m.Path, "is_file", return_value=True), \
                patch.object(m.Path, "read_bytes", return_value=plistlib.dumps(dict(CFBundleShortVersionString="4.59.0"))), \
                patch.object(m, "get_bytes", side_effect=m.CheckError("offline")), \
                patch.object(m, "get_json", return_value=dict(version="4.60.0,1234")):
            self.assertIn("ripiego", m.desktop_rows()[0]["detail"])

    def test_brew_uses_fresh_catalog_and_no_update_commands(self):
        inventory = dict(formulae=[dict(name="restic", tap="homebrew/core", installed=[dict(version="0.18.0")], pinned=True),
                                   dict(name="custom", tap="vendor/tap", installed=[dict(version="1.0")])],
                         casks=[dict(token="test-app", tap="homebrew/cask", installed="1.0", auto_updates=True)])
        def catalog(url):
            return dict(versions=dict(stable="0.18.0"), revision=1) if "/formula/" in url else dict(version="1.1")
        with patch.object(m, "tool", return_value="/fake/brew"), \
                patch.object(m, "command_json", return_value=inventory) as command, patch.object(m, "get_json", side_effect=catalog):
            rows = m.brew_rows()
        self.assertEqual([r["status"] for r in rows], ["UPDATE", "SKIP", "UPDATE"])
        self.assertIn("pinned", rows[0]["detail"])
        self.assertIn("auto-update", rows[2]["detail"])
        self.assertEqual(command.call_args.args[0], ["/fake/brew", "info", "--json=v2", "--installed"])

    def test_optional_brew_missing_and_bad_inventory(self):
        with patch.object(m, "tool", return_value=None):
            self.assertEqual(m.brew_rows()[0]["status"], "SKIP")
        with patch.object(m, "tool", return_value="/brew"), patch.object(m, "command_json", return_value=[]):
            with self.assertRaises(m.CheckError):
                m.brew_rows()

    def test_token_not_in_curl_argv_and_response_must_be_true(self):
        result = m.summary([m.row("Host", "x", "OK")])
        with patch.object(m.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"ok":true}\n200')) as run:
            self.assertTrue(m.send_push("https://kuma.test/api/push/SECRET_TOKEN", result))
        self.assertNotIn("SECRET_TOKEN", repr(run.call_args.args))
        self.assertIn("--globoff", run.call_args.args[0])
        self.assertNotIn("--fail", run.call_args.args[0])
        self.assertNotIn("--location", run.call_args.args[0])
        self.assertNotIn("--insecure", run.call_args.args[0])
        self.assertIn("SECRET_TOKEN", run.call_args.kwargs["input"])
        for response in ['{"ok":false}\n200', '{"ok":"true"}\n200', '<html/>\n200', '{"ok":true}\n302']:
            with patch.object(m.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=response)):
                with self.assertRaises(m.CheckError):
                    m.send_push("https://kuma.test/api/push/SECRET_TOKEN", result)

    def test_push_errors_distinguish_public_proxy_dns_tls_and_monitor_rejection(self):
        cases = [(6, "\n000", "DNS del dominio"), (7, "\n000", "connessione"),
                 (28, "\n000", "timeout"), (60, "\n000", "certificato TLS"),
                 (0, "<html>PRIVATE_TOKEN</html>\n302", "reindirizzamento HTTP 302"),
                 (0, "<html>PRIVATE_TOKEN</html>\n403", "accesso negato HTTP 403"),
                 (0, "<html>PRIVATE_TOKEN</html>\n401", "accesso negato HTTP 401"),
                 (0, "<html>PRIVATE_TOKEN</html>\n200", "pagina HTML"),
                 (0, '{"ok":false,"msg":"Monitor not found or not active."}\n404', "monitor Push non trovato"),
                 (0, "<html>PRIVATE_TOKEN</html>\n404", "percorso Push non trovato"),
                 (0, "PRIVATE_TOKEN\n429", "troppe richieste"), (0, "PRIVATE_TOKEN\n502", "server/proxy"),
                 (0, '{"ok":false,"msg":"PRIVATE_TOKEN"}\n200', "non conferma ok=true"),
                 (0, '{"ok":true}\n404', "percorso Push non trovato"),
                 (0, '[]\n200', "risposta JSON assente"), (0, "PRIVATE_TOKEN", "nessuna risposta HTTP valida")]
        for code, output, expected in cases:
            with self.subTest(code=code, expected=expected), self.assertRaises(m.CheckError) as raised:
                m.check_push_response(code, output)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("PRIVATE_TOKEN", str(raised.exception))

    def test_push_process_failures_never_disclose_url_from_exceptions(self):
        for error, expected in [(subprocess.TimeoutExpired("https://kuma.test/api/push/SECRET_TOKEN", 20), "timeout"),
                                (OSError("SECRET_TOKEN"), "impossibile avviare")]:
            with patch.object(m.subprocess, "run", side_effect=error), self.assertRaises(m.CheckError) as raised:
                m.send_push("https://kuma.test/api/push/SECRET_TOKEN", dict(status="up", message="real result"))
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("SECRET_TOKEN", str(raised.exception))

    def test_push_url_strips_query_but_rejects_injection(self):
        with patch.dict(os.environ, {"KUMA_UPDATECHECK_PUSH_URL": ""}):
            path = self.scripts / "updatecheck-push-url.txt"
            path.write_text("https://kuma.test/api/push/PRIVATE?status=up&ping=\n")
            self.assertEqual(m.push_url(self.scripts), "https://kuma.test/api/push/PRIVATE")
            path.write_text('https://kuma.test/api/push/PRIVATE\noutput = "/tmp/stolen"')
            with self.assertRaises(m.CheckError):
                m.push_url(self.scripts)

    def test_push_url_accepts_clipboard_wrappers_bom_and_windows_newline(self):
        expected = "https://kuma.test:3001/api/push/PRIVATE_123"
        for value in [expected, '\ufeff' + expected + '\r\n', ' "' + expected + '?status=up&msg=OK&ping="\n',
                      "'" + expected + "'", '<' + expected + '>', '“' + expected + '”', '`' + expected + '`']:
            with self.subTest(value=value), patch.dict(os.environ, {"KUMA_UPDATECHECK_PUSH_URL": ""}):
                (self.scripts / "updatecheck-push-url.txt").write_text(value, encoding="utf-8")
                self.assertEqual(m.push_url(self.scripts), expected)

    def test_push_url_handles_proxy_prefix_ipv6_and_trailing_slash(self):
        for original, expected in [
                ("https://kuma.test/monitor/kuma/api/push/PRIVATE/?status=up", "https://kuma.test/monitor/kuma/api/push/PRIVATE"),
                ("http://[::1]:3001/api/push/PRIVATE#fragment", "http://[::1]:3001/api/push/PRIVATE"),
                ("HTTPS://kuma.test/api/push/PRIVATE?msg=OK", "https://kuma.test/api/push/PRIVATE")]:
            self.assertEqual(m.normalize_push_url(original, "test"), expected)

    def test_push_url_reports_wrong_source_and_keeps_credentials_private(self):
        with patch.dict(os.environ, {"KUMA_UPDATECHECK_PUSH_URL": "https://kuma.test/api/wrong/PRIVATE_TOKEN"}):
            (self.scripts / "updatecheck-push-url.txt").write_text("https://kuma.test/api/push/VALID_TOKEN")
            with self.assertRaises(m.CheckError) as raised:
                m.push_url(self.scripts)
            self.assertIn("variabile KUMA_UPDATECHECK_PUSH_URL", str(raised.exception))
            self.assertNotIn("PRIVATE_TOKEN", str(raised.exception))
        with patch.dict(os.environ, {"KUMA_UPDATECHECK_PUSH_URL": ""}):
            (self.scripts / "updatecheck-push-url.txt").write_text("kuma.test/api/push/PRIVATE_TOKEN")
            with self.assertRaises(m.CheckError) as raised:
                m.push_url(self.scripts)
            self.assertIn("file updatecheck-push-url.txt", str(raised.exception))
            self.assertIn("http://", str(raised.exception))
            self.assertNotIn("PRIVATE_TOKEN", str(raised.exception))

    def test_push_url_rejects_malformed_urls_and_multiline_commands_without_leaking(self):
        invalid = ["https://kuma.test", "https://kuma.test/api/push/", "https://kuma.test/api/push/PRIVATE/extra",
                   "https://user:SECRET@kuma.test/api/push/PRIVATE", "https://kuma.test:bogus/api/push/PRIVATE",
                   "https://kuma.test:65536/api/push/PRIVATE", "https://[::1/api/push/PRIVATE",
                   "https://kuma.test/api/push/PRIVATE?status=up\nurl = https://other.test/api/push/SECRET",
                   'https://kuma.test/api/push/PRIVATE\\"', "curl https://kuma.test/api/push/PRIVATE",
                   "https://ku ma.test/api/push/PRIVATE", "https://kuma.test/api/push/PRIVATE\x00",
                   "https://ku\tma.test/api/push/PRIVATE", "https://kuma.test/api/push/PRIVATE\nsecond line",
                   "https://ku{ma,mb}.test/api/push/PRIVATE", r"{\rtf1 PRIVATE}"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(m.CheckError) as raised:
                m.normalize_push_url(value, "test")
            self.assertNotIn("PRIVATE", str(raised.exception))
            self.assertNotIn("SECRET", str(raised.exception))

    def test_push_url_invalid_encoding_is_actionable_and_missing_file_stays_optional(self):
        with patch.dict(os.environ, {"KUMA_UPDATECHECK_PUSH_URL": ""}):
            self.assertEqual(m.push_url(self.scripts), "")
            (self.scripts / "updatecheck-push-url.txt").write_bytes(b"\xff\xfe\x00\x00")
            with self.assertRaisesRegex(m.CheckError, "UTF-8"):
                m.push_url(self.scripts)

    def test_self_check_reports_configuration_without_printing_url_or_token(self):
        for value, expected in [("https://kuma.test/api/push/PRIVATE_TOKEN", "formato valido"), ("", "non configurato")]:
            capture = io.StringIO()
            with patch.object(m, "runtime_check"), patch.object(m.sys, "platform", "darwin"), \
                    patch.object(m.sys, "argv", ["update_checker.py", "--self-check"]), \
                    patch.object(m, "configured", return_value=self.cfg), patch.object(m, "push_url", return_value=value), \
                    redirect_stdout(capture):
                self.assertEqual(m.main(), 0)
            self.assertIn(expected, capture.getvalue())
            self.assertNotIn("PRIVATE_TOKEN", capture.getvalue())
            self.assertNotIn("https://kuma.test", capture.getvalue())

    def test_report_saved_private_even_if_push_fails(self):
        with patch.object(m, "collect", return_value=[m.row("Host", "x", "UPDATE")]), \
                patch.object(m, "push_url", return_value="https://kuma.test/api/push/SECRET"), \
                patch.object(m, "send_push", side_effect=m.CheckError("Kuma non raggiungibile")):
            self.assertEqual(m.run_scan(self.cfg, script_dir=self.scripts, root=self.root), 2)
        log = self.root / "logs/system/updatecheck"
        data = json.loads((log / "latest.json").read_text())
        self.assertEqual(data["result"]["updates"], 1)
        self.assertFalse(data["kuma_sent"])
        self.assertIn("push_error", data)
        self.assertNotIn("SECRET", json.dumps(data))
        self.assertEqual(len(list(log.glob("report-*.json"))), 1)
        self.assertEqual((log / "latest.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("Kuma non raggiungibile", (log / "latest.txt").read_text())

    def saved_scan(self, status="UPDATE", age_seconds=30):
        with patch.object(m, "collect", return_value=[m.row("Host", "x", status)]):
            m.run_scan(self.cfg, script_dir=self.scripts, root=self.root, no_push=True)
        path = self.scripts / "state/last-result.json"
        report = json.loads(path.read_text())
        report["finished_at"] = (dt.datetime.now().astimezone() - dt.timedelta(seconds=age_seconds)).isoformat()
        report["push_error"] = "previous failure"
        path.write_text(json.dumps(report))
        return report

    def test_retry_push_uses_real_saved_down_result_without_rescanning_or_changing_age(self):
        original = self.saved_scan()
        with patch.object(m, "collect", side_effect=AssertionError("must not rescan")), \
                patch.object(m, "push_url", return_value="https://kuma.test/api/push/PRIVATE_TOKEN"), \
                patch.object(m, "send_push", return_value=True) as push:
            code = m.retry_kuma(self.cfg, script_dir=self.scripts, root=self.root)
        self.assertEqual(code, 1)
        self.assertEqual(push.call_args.args[1]["status"], "down")
        self.assertIn("Scansione ", push.call_args.args[1]["message"])
        saved = json.loads((self.scripts / "state/last-result.json").read_text())
        self.assertTrue(saved["kuma_sent"])
        self.assertNotIn("push_error", saved)
        self.assertEqual(saved["finished_at"], original["finished_at"])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(saved))

    def test_retry_preserves_error_diagnosis_in_readable_and_json_reports(self):
        self.saved_scan()
        with patch.object(m, "push_url", return_value="https://kuma.test/api/push/PRIVATE_TOKEN"), \
                patch.object(m, "send_push", side_effect=m.CheckError("accesso negato HTTP 403")):
            with self.assertRaisesRegex(m.CheckError, "HTTP 403"):
                m.retry_kuma(self.cfg, script_dir=self.scripts, root=self.root)
        saved = json.loads((self.scripts / "state/last-result.json").read_text())
        self.assertFalse(saved["kuma_sent"])
        self.assertIn("403", saved["push_error"])
        self.assertIn("403", (self.root / "logs/system/updatecheck/latest.txt").read_text())

    def test_retry_rejects_old_future_or_missing_results_without_sending_fake_up(self):
        with patch.object(m, "send_push") as push:
            with self.assertRaisesRegex(m.CheckError, "scansione completa"):
                m.retry_kuma(self.cfg, script_dir=self.scripts, root=self.root)
            for age in [901, -120]:
                self.saved_scan(status="OK", age_seconds=age)
                with self.assertRaisesRegex(m.CheckError, "15 minuti"):
                    m.retry_kuma(self.cfg, script_dir=self.scripts, root=self.root)
            push.assert_not_called()

    def test_retry_refuses_overlap_with_running_scan(self):
        self.saved_scan()
        with (self.scripts / "state/.lock").open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(m.CheckError, "in esecuzione"):
                m.retry_kuma(self.cfg, script_dir=self.scripts, root=self.root)

    def test_no_push_saves_report_without_contacting_kuma(self):
        with patch.object(m, "collect", return_value=[m.row("Host", "x", "OK")]), \
                patch.object(m, "send_push") as push:
            self.assertEqual(m.run_scan(self.cfg, script_dir=self.scripts, root=self.root, no_push=True), 0)
            push.assert_not_called()
        self.assertTrue((self.scripts / "state/last-result.json").is_file())

    def test_concurrent_scan_and_symlink_report_rejected(self):
        state = self.scripts / "state"
        state.mkdir()
        with (state / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(m.CheckError, "già in esecuzione"):
                m.run_scan(self.cfg, script_dir=self.scripts, root=self.root, no_push=True)
        target = self.root / "private"
        target.write_text("preserve")
        link = self.root / "report-link"
        link.symlink_to(target)
        with self.assertRaises(m.CheckError):
            m.atomic_write(link, "overwrite")
        self.assertEqual(target.read_text(), "preserve")

    def test_subprocess_errors_do_not_disclose_stderr_or_arguments(self):
        with self.assertRaises(m.CheckError) as raised:
            m.command([sys.executable, "-c", "import sys; sys.stderr.write('PRIVATE_TOKEN'); sys.exit(7)"])
        self.assertNotIn("PRIVATE_TOKEN", str(raised.exception))
        with self.assertRaisesRegex(m.CheckError, "Timeout"):
            m.command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)

    def test_command_categories_do_not_copy_credentials_from_stderr(self):
        for args, stderr, expected in [
                (["git", "fetch"], b"Permission denied (publickey). PRIVATE_TOKEN", "autenticazione remota"),
                (["git", "fetch"], b"fatal: detected dubious ownership PRIVATE_TOKEN", "proprietario"),
                (["git", "fetch"], b"Host key verification failed PRIVATE_TOKEN", "chiave host"),
                (["git", "fetch"], b"fatal: couldn't find remote ref PRIVATE_TOKEN", "branch remoto"),
                (["docker"], b"error getting credentials: PRIVATE_TOKEN", "portachiavi"),
                (["docker"], b"manifest unknown: PRIVATE_TOKEN", "tag o digest"),
                (["docker"], b"pull access denied for PRIVATE_TOKEN", "registry negato"),
                (["docker"], b"toomanyrequests: PRIVATE_TOKEN", "limite di richieste")]:
            error = m.command_failure(args, 1, stderr)
            self.assertIn(expected, str(error))
            self.assertNotIn("PRIVATE_TOKEN", str(error))

    def test_command_preserves_caller_ssh_command_and_uses_real_user_home(self):
        with patch.dict(os.environ, dict(GIT_SSH_COMMAND="ssh -i /tmp/custom-deploy-key", HOME="/invalid/home")):
            out, _ = m.command([sys.executable, "-c", "import json,os,sys; print(json.dumps([os.environ['GIT_SSH_COMMAND'],os.environ['HOME'],sys.stdin.read()]))"])
        self.assertEqual(json.loads(out), ["ssh -i /tmp/custom-deploy-key", pwd.getpwuid(os.getuid()).pw_dir, ""])

    def test_diagnostic_mode_does_not_scan_apps_save_reports_or_contact_kuma(self):
        capture = io.StringIO()
        with patch.object(m, "runtime_check"), patch.object(m.sys, "platform", "darwin"), \
                patch.object(m.sys, "argv", ["update_checker.py", "--diagnose-sources"]), \
                patch.object(m, "configured", return_value=self.cfg), \
                patch.object(m, "docker_rows", return_value=[m.row("Docker", "backend", "SKIP", "fithub-backend:local", detail="Build locale")]), \
                patch.object(m, "git_rows", return_value=[m.row("Git", "FitHub", "ERROR", detail="Git: autenticazione")]), \
                patch.object(m, "application_rows") as apps, patch.object(m, "send_push") as push, \
                patch.object(m, "save_report") as save, redirect_stdout(capture):
            self.assertEqual(m.main(), 2)
        apps.assert_not_called()
        push.assert_not_called()
        save.assert_not_called()
        self.assertIn("[ERROR] Git", capture.getvalue())
        self.assertIn("fithub-backend:local", capture.getvalue())

    def test_git_inventory_uses_local_head_only_without_network_or_credentials(self):
        checkout = self.root / "FitHub"
        subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
        for key, value in [("user.name", "Test"), ("user.email", "test@example.test")]:
            subprocess.run(["git", "-C", str(checkout), "config", key, value], check=True, capture_output=True)
        (checkout / "file").write_text("initial")
        subprocess.run(["git", "-C", str(checkout), "add", "file"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-m", "initial"], check=True, capture_output=True)
        (checkout / "file").write_text("my local modification")
        with patch.object(m, "command", wraps=m.command) as command, patch.object(m, "get_json") as get:
            rows = m.git_rows(self.cfg["git"], self.root)
        self.assertEqual(rows[0]["status"], "SKIP")
        self.assertEqual(len(rows[0]["installed"]), 12)
        self.assertIn("remoti non verificati", rows[0]["detail"])
        self.assertEqual(len(command.call_args_list), 1)
        self.assertEqual(command.call_args.args[0][-2:], ["rev-parse", "HEAD"])
        self.assertEqual((checkout / "file").read_text(), "my local modification")
        self.assertFalse((checkout / ".git/FETCH_HEAD").exists())
        get.assert_not_called()

    def test_git_rejects_path_escape_and_absent_checkout_is_explicit(self):
        cfg = dict(self.cfg["git"], path="../outside")
        with self.assertRaises(m.CheckError):
            m.git_rows(cfg, self.root)
        self.assertEqual(m.git_rows(self.cfg["git"], self.root)[0]["status"], "SKIP")


@unittest.skipUnless(sys.platform == "darwin", "Native macOS plutil/install")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "Fithub & user's project"
        self.scripts = self.root / "scripts/updatecheck"
        shutil.copytree(SOURCE, self.scripts)
        self.plists = self.base / "LaunchDaemons"
        self.plists.mkdir()
        self.events = self.base / "events"
        self.logs = self.root / "logs/system/updatecheck"
        self.url = self.scripts / "updatecheck-push-url.txt"
        self.url.write_text("https://kuma.test/api/push/PRIVATE_TOKEN\n")
        # Runtime preflight is stubbed; this host intentionally has a different Python.
        (self.scripts / "update-check.sh").write_text("#!/bin/bash\nexit 0\n")
        fake_install = self.base / "install"
        fake_install.write_text(f'#!{sys.executable}\nimport subprocess,sys\na=[]; i=1\nwhile i<len(sys.argv):\n if sys.argv[i] in ("-o","-g"): i+=2; continue\n a.append(sys.argv[i]); i+=1\nsys.exit(subprocess.run(["/usr/bin/install",*a]).returncode)\n')
        fake_chown = self.base / "chown"
        fake_chown.write_text("#!/bin/sh\nexit 0\n")
        fake_launch = self.base / "launchctl"
        fake_launch.write_text(f'''#!{sys.executable}
import json,os,pathlib,sys
p=pathlib.Path({str(self.events)!r})
calls=[json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
with p.open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
started=any(x[0]=='kickstart' for x in calls)
if sys.argv[1]=='print':
 if not started: sys.exit(1)
 if os.environ.get('TEST_SPAWN_FAIL'): print('job state = spawn failed')
 elif os.environ.get('TEST_RUNNING'): print('state = running')
 else: print('state = not running\\nlast exit code = '+os.environ.get('TEST_CODE','0'))
if sys.argv[1]=='kickstart' and not os.environ.get('TEST_NO_LOG'):
 with pathlib.Path({str(self.logs / 'update-check.log')!r}).open('a') as f: f.write('Controllo Docker...\\n')
''')
        for file in [fake_install, fake_chown, fake_launch]:
            file.chmod(0o700)
        source = (SOURCE / "install-updatecheck-service.sh").read_text()
        source = source.replace("if (( EUID != 0 )); then", "if false; then").replace("/bin/sleep 1", ":")
        source = source.replace("/Library/LaunchDaemons", str(self.plists))
        for original, replacement in [("/usr/bin/install", fake_install), ("/usr/sbin/chown", fake_chown), ("/bin/launchctl", fake_launch)]:
            source = source.replace(original, shlex.quote(str(replacement)))
        (self.scripts / "install-updatecheck-service.sh").write_text(source)

    def run_installer(self, **extra):
        return subprocess.run(["/bin/bash", str(self.scripts / "install-updatecheck-service.sh")],
                              capture_output=True, text=True, timeout=15,
                              env=dict(os.environ, SUDO_USER=pwd.getpwuid(os.getuid()).pw_name, **extra))

    def test_calendar_canonical_path_permissions_and_initial_launch(self):
        result = self.run_installer(TEST_RUNNING="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("in corso", result.stdout)
        plist = plistlib.loads((self.plists / "ch.nmperformance.fithub.updatecheck.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], ["/bin/bash", str(self.scripts / "update-check.sh"), "--service"])
        self.assertEqual(plist["StartCalendarInterval"], dict(Weekday=1, Hour=19, Minute=0))
        for key in ["WorkingDirectory", "StandardOutPath", "StandardErrorPath", "RunAtLoad"]:
            self.assertNotIn(key, plist)
        self.assertEqual(self.url.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("PRIVATE_TOKEN", result.stdout + result.stderr)

    def test_preserves_configuration_and_applies_custom_calendar(self):
        cfg = json.loads((self.scripts / "updatecheck.example.json").read_text())
        cfg["schedule"] = dict(weekday=3, hour=8, minute=25)
        cfg["docker"]["exclude"] = ["custom"]
        config = self.scripts / "updatecheck.json"
        config.write_text(json.dumps(cfg))
        original = config.read_text()
        result = self.run_installer(TEST_CODE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(config.read_text(), original)
        plist = plistlib.loads((self.plists / "ch.nmperformance.fithub.updatecheck.plist").read_bytes())
        self.assertEqual(plist["StartCalendarInterval"], dict(Weekday=3, Hour=8, Minute=25))

    def test_spawn_failure_or_no_new_sample_not_claimed_success(self):
        result = self.run_installer(TEST_SPAWN_FAIL="1")
        self.assertEqual(result.returncode, 2)
        self.events.unlink()
        result = self.run_installer(TEST_NO_LOG="1")
        self.assertEqual(result.returncode, 2)

    def test_missing_token_fails_before_registering_service(self):
        self.url.unlink()
        result = self.run_installer()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.events.exists())

    def test_incomplete_scan_is_reported(self):
        result = self.run_installer(TEST_CODE="2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("scansione incompleta", result.stderr)

    def test_service_wrapper_opens_logs_inside_project_after_start(self):
        self.logs.mkdir(parents=True)
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        python = fake_bin / "python3.14"
        python.write_text('#!/bin/bash\necho "started: $*"\necho "test stderr" >&2\n')
        python.chmod(0o700)
        source = (SOURCE / "update-check.sh").read_text()
        source = "\n".join('export PATH="$TEST_BIN:/usr/bin:/bin:/usr/sbin:/sbin"' if line.startswith("export PATH=") else line
                           for line in source.splitlines()) + "\n"
        wrapper = self.scripts / "update-check.sh"
        wrapper.write_text(source)
        result = subprocess.run(["/bin/bash", str(wrapper), "--service"], capture_output=True, text=True,
                                env=dict(os.environ, TEST_BIN=str(fake_bin)), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("--service", (self.logs / "update-check.log").read_text())
        self.assertEqual((self.logs / "update-check-error.log").read_text(), "test stderr\n")


if __name__ == "__main__":
    unittest.main()
