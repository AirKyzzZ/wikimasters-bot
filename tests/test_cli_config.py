import contextlib
import io
import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REFRESH_PATH, Reply, ServerTestCase, fresh_tokens, session
from wmbot import cli
from wmbot.config import ConfigError, load_config, parse_env_file
from wmbot.net import RedactingFilter, redact
from wmbot.session import ProcessLock, SessionStore


class RedactTests(unittest.TestCase):
    def test_redacts_all_token_shapes(self):
        samples = {
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefghijkl",
            "field": '{"refresh_token":"abc123"}',
            "form": "refresh_token=abc123&x=1",
            "cookie": "sb-ref-auth-token.0=base64-abc123; Path=/",
            "hook": "https://discord.com/api/webhooks/1/abc123",
        }
        for name, sample in samples.items():
            with self.subTest(name):
                out = redact(sample)
                self.assertNotIn("abc123", out)
                self.assertNotIn("eyJzdWIi", out)

    def test_filter_applies_to_formatted_args(self):
        record = logging.LogRecord("x", logging.INFO, "f", 1, "got %s", ('refresh_token="abc123"',), None)
        RedactingFilter().filter(record)
        self.assertNotIn("abc123", record.getMessage())


class ConfigTests(unittest.TestCase):
    def test_defaults_keep_existing_secret_path(self):
        config = load_config({}, Path("/nonexistent"))
        self.assertEqual(config.session_file, Path.home() / ".secrets/wikimasters.session.json")

    def test_rejects_plain_http_remote_urls(self):
        with self.assertRaises(ConfigError):
            load_config({"WM_SITE_URL": "http://evil.example"}, Path("/nonexistent"))
        with self.assertRaises(ConfigError):
            load_config({"WM_SUPABASE_URL": "ftp://x"}, Path("/nonexistent"))

    def test_rejects_non_discord_webhook_without_echoing_it(self):
        with self.assertRaises(ConfigError) as caught:
            load_config({"WM_DISCORD_WEBHOOK": "https://evil.example/secret"}, Path("/nonexistent"))
        self.assertNotIn("secret", str(caught.exception))
        ok = "https://discord.com/api/webhooks/1/x"
        self.assertEqual(load_config({"WM_DISCORD_WEBHOOK": ok}, Path("/nonexistent")).discord_webhook, ok)

    def test_env_file_parsing_and_precedence(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write('# comment\nexport WM_DATA_DIR="/a b"\nWM_ANON_KEY=k=v\nWM_USER_AGENT=\nbad line\n')
        self.addCleanup(os.unlink, handle.name)
        parsed = parse_env_file(Path(handle.name))
        self.assertEqual(parsed["WM_DATA_DIR"], "/a b")
        self.assertEqual(parsed["WM_ANON_KEY"], "k=v")
        config = load_config({"WM_ANON_KEY": "real"}, Path(handle.name))
        self.assertEqual(config.anon_key, "real")
        self.assertEqual(config.data_dir, Path("/a b"))
        self.assertIn("wmbot", config.user_agent)


class CliTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        env = {
            "WM_SESSION_FILE": str(self.config.session_file),
            "WM_DATA_DIR": str(self.config.data_dir),
            "WM_SITE_URL": self.mock.url,
            "WM_SUPABASE_URL": self.mock.url,
            "WM_ANON_KEY": "fake-anon",
            "WM_ENV_FILE": "/nonexistent",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_login_from_stdin_exchanges_and_saves_0600_without_printing(self):
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens(1)))
        with mock.patch("sys.stdin", io.StringIO("pasted-token\n")):
            code, out, err = self.run_cli("login")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.mock.hits(REFRESH_PATH)[0]["body"])["refresh_token"], "pasted-token")
        self.assertEqual(stat.S_IMODE(self.config.session_file.stat().st_mode), 0o600)
        for text in (out, err):
            self.assertNotIn("pasted-token", text)
            self.assertNotIn("fake-refresh-1", text)

    def test_login_refuses_to_overwrite_without_force(self):
        SessionStore(self.config.session_file).save(session())
        with mock.patch("sys.stdin", io.StringIO("x\n")):
            self.assertEqual(self.run_cli("login")[0], 1)
        self.assertEqual(self.mock.hits(REFRESH_PATH), [])

    def test_login_rejected_token_saves_nothing_and_exits_2(self):
        self.mock.on(REFRESH_PATH, Reply(400, {"error_code": "refresh_token_not_found"}))
        with mock.patch("sys.stdin", io.StringIO("bad\n")):
            self.assertEqual(self.run_cli("login")[0], 2)
        self.assertFalse(self.config.session_file.exists())

    def test_login_blank_token(self):
        with mock.patch("sys.stdin", io.StringIO("\n")):
            self.assertEqual(self.run_cli("login")[0], 1)

    def test_run_exits_3_when_lock_held(self):
        SessionStore(self.config.session_file).save(session())
        with ProcessLock(self.config.lock_file), mock.patch("wmbot.cli.LOCK_RETRY_SECONDS", 0):
            self.assertEqual(self.run_cli("run")[0], 3)

    def test_run_exits_2_on_dead_session_and_keeps_file(self):
        SessionStore(self.config.session_file).save(session(1))
        self.mock.on(REFRESH_PATH, Reply(400, {"error_code": "refresh_token_already_used"}))
        self.assertEqual(self.run_cli("run")[0], 2)
        self.assertTrue(self.config.session_file.exists())

    def test_run_missing_session_exits_1_with_hint(self):
        code, _, err = self.run_cli("run")
        self.assertEqual(code, 1)
        self.assertIn("wmbot login", err)

    def test_status_never_prints_tokens(self):
        SessionStore(self.config.session_file).save(session())
        for args in (["status"], ["status", "--json"]):
            code, out, _ = self.run_cli(*args)
            self.assertEqual(code, 0)
            self.assertNotIn("fake", out)
        self.assertIn("not running", self.run_cli("status")[1])

    def test_status_without_anything(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("wmbot login", out)

    def test_report_stdout_and_file(self):
        code, out, _ = self.run_cli("report", "--date", "2026-11-03", "--stdout")
        self.assertEqual(code, 0)
        self.assertIn("Nothing opened", out)
        code, out, _ = self.run_cli("report", "--date", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertTrue((self.config.reports_dir / "2026-11-03.md").exists())

    def test_report_bad_date_exits_1(self):
        self.assertEqual(self.run_cli("report", "--date", "tomorrow")[0], 1)

    def test_report_discord_without_webhook_exits_1(self):
        self.assertEqual(self.run_cli("report", "--discord")[0], 1)
