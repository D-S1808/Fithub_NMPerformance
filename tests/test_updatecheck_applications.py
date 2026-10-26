"""App inventory and remote metadata. No real network or application execution."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "scripts/updatecheck"
spec = importlib.util.spec_from_file_location("apps_checker", SOURCE / "update_checker.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.apps = self.root / "Applications"
        self.apps.mkdir()
        self.cfg = dict(enabled=True, directories=[str(self.apps)], app_store_country="ch", cask_overrides={})

    def make_app(self, name="Example", identifier="org.example.app", version="1.0", build="100", feed="", store=False, parent=None):
        app = (parent or self.apps) / (name + ".app")
        (app / "Contents").mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(dict(CFBundleName=name, CFBundleIdentifier=identifier,
                    CFBundleShortVersionString=version, CFBundleVersion=build, SUFeedURL=feed)))
        if store:
            (app / "Contents/_MASReceipt").mkdir()
            (app / "Contents/_MASReceipt/receipt").write_bytes(b"receipt")
        return app

    def app(self, **kw):
        self.make_app(**kw)
        return m.application_inventory([str(self.apps)])[0][0]

    def cask(self, **kw):
        result = dict(token="example-app", version="1.2", bundle_version=None, bundle_short_version=None,
                      artifacts=[dict(app=["Example.app"]), dict(uninstall=[dict(quit="org.example.app")])], depends_on={})
        result.update(kw)
        return result

    def run_apps(self, catalog):
        with patch.object(m, "command", return_value=("27.0.1", "")), patch.object(m, "get_json", return_value=catalog) as fetch:
            rows = m.application_rows(self.cfg)
        return rows, fetch

    def test_recursive_inventory_includes_versions_and_preserves_duplicate_installs(self):
        self.make_app()
        other = self.apps / "Tools"
        other.mkdir()
        self.make_app(parent=other, version="2.0")
        apps, errors = m.application_inventory([str(self.apps), str(other)])
        self.assertEqual(len(apps), 2)
        self.assertEqual(sorted(a["version"] for a in apps), ["1.0", "2.0"])
        self.assertEqual(errors, [])

    def test_nested_helpers_are_not_counted_as_independent_programs(self):
        outer = self.make_app()
        self.make_app(name="Updater", parent=outer / "Contents/Helpers")
        self.assertEqual(len(m.application_inventory([str(self.apps)])[0]), 1)

    def test_unreadable_app_is_error_and_does_not_hide_other_apps(self):
        self.make_app()
        (self.apps / "Broken.app").mkdir()
        apps, errors = m.application_inventory([str(self.apps)])
        self.assertEqual(len(apps), 1)
        self.assertEqual(errors[0]["status"], "ERROR")
        self.assertTrue(errors[0]["app_path"].endswith("Broken.app"))

    def test_unknown_app_is_listed_with_installed_version_and_path(self):
        path = self.make_app()
        rows, _ = self.run_apps([])
        self.assertEqual(rows[0]["status"], "SKIP")
        self.assertEqual(rows[0]["installed"], "1.0")
        self.assertEqual(rows[0]["app_path"], str(path))
        self.assertEqual(m.summary(rows)["applications_unverified"], 1)
        self.assertEqual(m.summary(rows)["status"], "down")  # No actual checks completed.

    def test_cask_matches_bundle_identity_and_name_without_brew_installed(self):
        self.make_app()
        with patch.object(m, "tool", side_effect=AssertionError("must not execute brew")):
            rows, fetch = self.run_apps([self.cask()])
        self.assertEqual(rows[0]["status"], "UPDATE")
        self.assertEqual(rows[0]["available"], "1.2")
        fetch.assert_called_once()
        self.assertIn("cask.json", fetch.call_args.args[0])

    def test_catalog_downloaded_once_for_all_apps(self):
        self.make_app()
        self.make_app(name="Other", identifier="org.other.app")
        _, fetch = self.run_apps([self.cask()])
        fetch.assert_called_once()

    def test_cask_never_matches_only_a_similar_application_name(self):
        self.make_app(identifier="org.unrelated.app")
        self.assertEqual(self.run_apps([self.cask()])[0][0]["status"], "SKIP")

    def test_catalog_preferences_can_identify_apps_with_no_quit_field(self):
        self.make_app()
        cask = self.cask(artifacts=[dict(app=["Example.app"]), dict(zap=[dict(trash=["~/Library/Preferences/org.example.app.plist"])])])
        self.assertEqual(self.run_apps([cask])[0][0]["status"], "UPDATE")

    def test_ambiguous_channels_require_manual_mapping(self):
        self.make_app()
        catalog = [self.cask(), self.cask(token="example-beta", version="2.0")]
        self.assertEqual(self.run_apps(catalog)[0][0]["status"], "SKIP")
        self.cfg["cask_overrides"] = {"org.example.app": "example-app"}
        self.assertEqual(self.run_apps(catalog)[0][0]["available"], "1.2")

    def test_actual_app_version_newer_than_catalog_is_ok(self):
        app = self.app(version="1.4")
        self.assertEqual(m.cask_application(app, self.cask(), "27.0")[0], "OK")

    def test_auto_updated_app_does_not_create_false_alert_from_stale_brew_receipt(self):
        app = self.make_app(version="1.5")
        inventory = dict(formulae=[], casks=[dict(token="example-app", tap="homebrew/cask", installed="1.0",
                                                 artifacts=[dict(app=["Example.app"])])])
        verified = [dict(app_path=str(app), status="OK")]
        with patch.object(m, "tool", return_value="/fake/brew"), patch.object(m, "command_json", return_value=inventory), \
                patch.object(m, "get_json") as catalog:
            rows = m.brew_rows(applications=verified)
        self.assertEqual(rows[0]["status"], "SKIP")
        self.assertIn("binario", rows[0]["detail"])
        catalog.assert_not_called()

    def test_inventory_source_and_result_are_saved_to_readable_and_json_reports(self):
        self.make_app()
        rows, _ = self.run_apps([self.cask()])
        scripts = self.root / "scripts/updatecheck"
        scripts.mkdir(parents=True)
        config = json.loads((SOURCE / "updatecheck.example.json").read_text())
        with patch.object(m, "collect", return_value=rows):
            self.assertEqual(m.run_scan(config, script_dir=scripts, root=self.root, no_push=True), 1)
        log = self.root / "logs/system/updatecheck"
        report = json.loads((log / "latest.json").read_text())
        self.assertEqual(report["result"]["applications_found"], 1)
        self.assertEqual(report["result"]["applications_unverified"], 0)
        self.assertIn("org.example.app", (log / "latest.txt").read_text())
        self.assertIn("Example.app", (log / "latest.txt").read_text())

    def test_cask_same_marketing_version_compares_internal_build(self):
        app = self.app(version="1.2", build="100")
        cask = self.cask(bundle_short_version="1.2", bundle_version="101")
        self.assertEqual(m.cask_application(app, cask, "27.0")[0], "UPDATE")

    def test_cask_full_build_is_not_compared_to_short_marketing_version(self):
        app = self.app(version="16.108", build="16.108.26092039")
        self.assertEqual(m.cask_application(app, self.cask(version="16.108.26092039"), "27.0")[0], "OK")
        self.assertEqual(m.cask_application(app, self.cask(version="16.108.26092038"), "27.0")[0], "OK")
        self.assertEqual(m.cask_application(app, self.cask(version="16.108.26092040"), "27.0")[0], "UPDATE")

    def test_unknown_version_never_claimed_outdated(self):
        app = self.app(version="1.2-beta")
        self.assertEqual(m.cask_application(app, self.cask(), "27.0")[0], "SKIP")
        self.assertEqual(m.app_version_status("2.0", "2"), "OK")

    def test_cask_incompatible_macos_requires_review(self):
        app = self.app()
        cask = self.cask(depends_on=dict(macos={">=": ["28"]}))
        self.assertEqual(m.cask_application(app, cask, "27.0")[0], "REVIEW")

    def feed(self, items):
        return ('<rss xmlns:s="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>' + items + '</channel></rss>').encode()

    def test_sparkle_uses_build_even_when_marketing_version_is_equal(self):
        app = self.app(feed="https://example.test/feed")
        feed = self.feed('<item><s:version>101</s:version><s:shortVersionString>1.0</s:shortVersionString><enclosure url="https://example.test/app.zip"/></item>')
        with patch.object(m, "get_bytes", return_value=feed):
            status, available, _ = m.sparkle_application(app, "27.0", "arm64")
        self.assertEqual(status, "UPDATE")
        self.assertEqual(available, "1.0")

    def test_sparkle_filters_betas_windows_deltas_and_incompatible_releases(self):
        app = self.app(feed="https://example.test/feed")
        feed = self.feed('''
          <item><enclosure s:version="100" s:shortVersionString="1.0"/></item>
          <item><s:channel>beta</s:channel><enclosure s:version="999"/></item>
          <item><enclosure s:version="998" s:os="windows"/></item>
          <item><s:minimumSystemVersion>28.0</s:minimumSystemVersion><enclosure s:version="997"/></item>
          <item><s:hardwareRequirements>x86_64</s:hardwareRequirements><enclosure s:version="996"/></item>
          <item><enclosure s:version="995" s:deltaFrom="100"/></item>
          <item><s:minimumUpdateVersion>101</s:minimumUpdateVersion><enclosure s:version="994"/></item>
        ''')
        with patch.object(m, "get_bytes", return_value=feed):
            self.assertEqual(m.sparkle_application(app, "27.0", "arm64")[0], "OK")

    def test_sparkle_bad_xml_and_no_compatible_feed_entries(self):
        app = self.app(feed="https://example.test/feed")
        with patch.object(m, "get_bytes", return_value=b"not xml"):
            with self.assertRaises(m.CheckError):
                m.sparkle_application(app, "27.0", "arm64")
        with patch.object(m, "get_bytes", return_value=self.feed("")):
            self.assertEqual(m.sparkle_application(app, "27.0", "arm64")[0], "SKIP")

    def test_dynamic_or_insecure_sparkle_url_not_contacted(self):
        app = self.app(feed="http://example.test/feed")
        with patch.object(m, "get_bytes") as fetch:
            self.assertEqual(m.sparkle_application(app, "27.0", "arm64")[0], "SKIP")
            fetch.assert_not_called()

    def test_store_requires_exact_mac_bundle_and_rejects_ios_record(self):
        app = self.app(store=True)
        record = dict(bundleId="org.example.app", kind="mac-software", version="1.3", minimumOsVersion="13.0")
        with patch.object(m, "get_json", return_value=dict(results=[record])) as fetch:
            self.assertEqual(m.store_application(app, "ch", "27.0")[0], "UPDATE")
            self.assertIn("country=ch", fetch.call_args.args[0])
        for changed in [dict(kind="software"), dict(bundleId="org.unrelated.app")]:
            with patch.object(m, "get_json", return_value=dict(results=[dict(record, **changed)])):
                self.assertEqual(m.store_application(app, "ch", "27.0")[0], "SKIP")

    def test_store_unavailable_or_unparseable_is_not_up_to_date(self):
        app = self.app(store=True)
        with patch.object(m, "get_json", side_effect=m.CheckError("Catalogo non raggiungibile")):
            with self.assertRaises(m.CheckError):
                m.store_application(app, "ch", "27.0")
        with patch.object(m, "get_json", return_value=dict(results=[])):
            self.assertEqual(m.store_application(app, "ch", "27.0")[0], "SKIP")

    def test_store_receipt_takes_priority_over_external_sparkle_feed(self):
        self.make_app(store=True, feed="https://example.test/external-appcast")
        with patch.object(m, "command", return_value=("27.0", "")), \
                patch.object(m, "sparkle_application") as sparkle, \
                patch.object(m, "store_application", return_value=("OK", "1.0", "store")) as store:
            self.assertEqual(m.application_rows(self.cfg)[0]["status"], "OK")
            store.assert_called_once()
            sparkle.assert_not_called()

    def test_store_rate_limit_and_error_isolation(self):
        self.make_app(name="First", store=True)
        self.make_app(name="Second", store=True)
        self.make_app(name="Unknown", identifier="org.unknown.app")
        with patch.object(m, "command", return_value=("27.0", "")), \
                patch.object(m, "get_json", return_value=[]), \
                patch.object(m, "store_application", side_effect=[m.CheckError("offline"), ("OK", "1.0", "store")]), \
                patch.object(m.time, "sleep") as sleep:
            rows = m.application_rows(self.cfg)
        self.assertEqual([r["status"] for r in rows], ["ERROR", "OK", "SKIP"])
        sleep.assert_called_once()

    def test_failed_catalog_is_not_retried_for_every_application(self):
        self.make_app()
        self.make_app(name="Second")
        with patch.object(m, "command", return_value=("27.0", "")), \
                patch.object(m, "get_json", side_effect=m.CheckError("offline")) as fetch:
            self.assertEqual([r["status"] for r in m.application_rows(self.cfg)], ["ERROR", "ERROR"])
            fetch.assert_called_once()

    def test_old_configuration_enables_app_scanning_without_overwriting_it(self):
        cfg = json.loads((SOURCE / "updatecheck.example.json").read_text())
        del cfg["applications"]
        path = self.root / "updatecheck.json"
        path.write_text(json.dumps(cfg))
        original = path.read_bytes()
        self.assertTrue(m.configured(self.root)["applications"]["enabled"])
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_application_configuration_rejected(self):
        original = json.loads((SOURCE / "updatecheck.example.json").read_text())
        for key, value in [("enabled", "true"), ("directories", ["relative"]), ("app_store_country", "switzerland"),
                           ("cask_overrides", {"org.app": "../../invalid"})]:
            cfg = copy.deepcopy(original)
            cfg["applications"][key] = value
            (self.root / "updatecheck.json").write_text(json.dumps(cfg))
            with self.assertRaises(m.CheckError):
                m.configured(self.root)

    def shortcuts_fixture(self, code=2, script=None):
        scripts = self.root / "scripts/updatecheck"
        scripts.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / "run-from-shortcuts.sh", scripts / "run-from-shortcuts.sh")
        (scripts / "update-check.sh").write_text(script or '#!/bin/bash\nprintf "%s\\n" "$*"\necho "ERRORE: diagnosi simulata" >&2\nexit ' + str(code) + '\n')
        return scripts

    def run_shortcuts(self, scripts, *args):
        return subprocess.run(["/bin/bash", str(scripts / "run-from-shortcuts.sh"), *args],
                              capture_output=True, text=True, timeout=5)

    def test_shortcuts_delivers_error_report_as_action_output_without_faking_success(self):
        for code, expected in [(0, 0), (1, 0), (2, 0), (78, 78), (127, 127)]:
            scripts = self.shortcuts_fixture(code)
            result = self.run_shortcuts(scripts, "--no-push")
            self.assertEqual(result.returncode, expected)
            self.assertIn("--no-push", result.stdout)
            self.assertIn("Codice checker: " + str(code), result.stdout)
            self.assertIn("diagnosi simulata", result.stdout)
            if code == 2:
                self.assertIn("Esito checker: ERRORE", result.stdout)
                self.assertIn("non viene convertito in UP", result.stdout)
            log = self.root / "logs/system/updatecheck/shortcut-last-run.log"
            self.assertEqual(log.read_text(), result.stdout)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_shortcuts_strict_exit_keeps_original_failure_code_and_error_text(self):
        scripts = self.shortcuts_fixture(2)
        result = self.run_shortcuts(scripts, "--strict-exit", "--no-push")
        self.assertEqual(result.returncode, 2)
        self.assertIn("diagnosi simulata", result.stdout)
        self.assertIn("--no-push", result.stdout)
        self.assertNotIn("--strict-exit --no-push", result.stdout)

    def test_shortcuts_includes_failed_checks_only_from_the_current_report(self):
        logs = self.root / "logs/system/updatecheck"
        logs.mkdir(parents=True)
        (logs / "latest.txt").write_text('[ERROR] Git / FitHub\n  autenticazione fallita\n\n[OK] Docker / postgres\n  immagine corrente\n\n')
        scripts = self.shortcuts_fixture(script='#!/bin/bash\necho "Report: ' + str(logs / "latest.txt") + '"\nexit 2\n')
        result = self.run_shortcuts(scripts)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[ERROR] Git", result.stdout)
        self.assertNotIn("[OK] Docker", result.stdout)
        self.assertEqual((logs / "latest.txt").read_text(), '[ERROR] Git / FitHub\n  autenticazione fallita\n\n[OK] Docker / postgres\n  immagine corrente\n\n')
        self.shortcuts_fixture(2)
        result = self.run_shortcuts(scripts)
        self.assertNotIn("[ERROR] Git", result.stdout)  # preflight failure must not echo stale data

    def test_shortcuts_keeps_real_down_result_and_kuma_confirmation_intact(self):
        scripts = self.shortcuts_fixture(2)
        config = json.loads((SOURCE / "updatecheck.example.json").read_text())
        capture = io.StringIO()
        with patch.object(m, "collect", return_value=[m.row("Git", "FitHub", "ERROR", detail="autenticazione remota fallita")]), \
                patch.object(m, "push_url", return_value="https://kuma.test/api/push/PRIVATE_TOKEN"), \
                patch.object(m, "send_push", return_value=True) as push, redirect_stdout(capture):
            self.assertEqual(m.run_scan(config, script_dir=scripts, root=self.root), 2)
        self.assertEqual(push.call_args.args[1]["status"], "down")
        logs = self.root / "logs/system/updatecheck"
        before = (logs / "latest.json").read_bytes()
        (scripts / "update-check.sh").write_text('#!/bin/bash\necho "Report: ' + str(logs / "latest.txt") + '"\nexit 2\n')
        result = self.run_shortcuts(scripts)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[ERROR] Git", result.stdout)
        self.assertIn("autenticazione remota fallita", result.stdout)
        self.assertEqual((logs / "latest.json").read_bytes(), before)
        self.assertEqual(json.loads(before)["result"]["status"], "down")
        self.assertTrue(json.loads(before)["kuma_sent"])

    def test_shortcuts_read_only_modes_do_not_create_log_files(self):
        scripts = self.shortcuts_fixture(0)
        for flag in ["--self-check", "--help", "--diagnose-sources"]:
            result = self.run_shortcuts(scripts, flag)
            self.assertEqual(result.returncode, 0)
            self.assertFalse((self.root / "logs").exists())

    def test_shortcuts_does_not_follow_a_log_symlink(self):
        scripts = self.shortcuts_fixture(2)
        logs = self.root / "logs/system/updatecheck"
        logs.mkdir(parents=True)
        private = self.root / "private-data"
        private.write_text("preserve")
        (logs / "shortcut-last-run.log").symlink_to(private)
        result = self.run_shortcuts(scripts)
        self.assertEqual(private.read_text(), "preserve")
        self.assertIn("log Shortcuts non salvato", result.stdout)
        self.assertIn("Esito checker: ERRORE", result.stdout)


if __name__ == "__main__":
    unittest.main()
