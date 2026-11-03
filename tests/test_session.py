import base64
import json
import logging
import os
import stat
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from tests.support import (
    FAKE_REFRESH,
    REFRESH_PATH,
    Reply,
    ServerTestCase,
    cookie_value,
    fresh_tokens,
    session,
)
from wmbot.net import NetworkError
from wmbot.session import (
    AlreadyRunning,
    Authenticator,
    FatalAuthError,
    ProcessLock,
    Session,
    SessionError,
    SessionStore,
    TransientAuthError,
    build_cookie,
    discover_anon_key,
    session_from_set_cookies,
)


class StoreTests(ServerTestCase):
    def test_save_is_0600_and_leaves_no_temp_file(self):
        store = SessionStore(self.config.session_file)
        store.save(session())
        self.assertEqual(stat.S_IMODE(self.config.session_file.stat().st_mode), 0o600)
        self.assertEqual(list(self.config.session_file.parent.iterdir()), [self.config.session_file])

    def test_round_trip(self):
        store = SessionStore(self.config.session_file)
        store.save(session(123))
        self.assertEqual(store.load(), session(123))

    def test_failed_write_keeps_previous_session(self):
        store = SessionStore(self.config.session_file)
        store.save(session(1))
        with mock.patch("wmbot.session.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                store.save(session(2))
        self.assertEqual(store.load().expires_at, 1)
        self.assertEqual(list(self.config.session_file.parent.iterdir()), [self.config.session_file])

    def test_load_tightens_loose_permissions(self):
        store = SessionStore(self.config.session_file)
        store.save(session())
        self.config.session_file.chmod(0o644)
        store.load()
        self.assertEqual(stat.S_IMODE(self.config.session_file.stat().st_mode), 0o600)

    def test_missing_and_corrupt_files_raise_without_content(self):
        store = SessionStore(self.config.session_file)
        with self.assertRaisesRegex(SessionError, "wmbot login"):
            store.load()
        self.config.session_file.parent.mkdir(parents=True)
        self.config.session_file.write_text('{"refresh_token": "secret-value"')
        with self.assertRaises(SessionError) as caught:
            store.load()
        self.assertNotIn("secret-value", str(caught.exception))

    def test_repr_hides_tokens(self):
        self.assertNotIn(FAKE_REFRESH, repr(session()))


class LockTests(ServerTestCase):
    def test_second_holder_is_refused_until_release(self):
        first = ProcessLock(self.tmp / "x.lock")
        second = ProcessLock(self.tmp / "x.lock")
        first.acquire()
        with self.assertRaises(AlreadyRunning):
            second.acquire()
        self.assertTrue(second.is_held_elsewhere())
        first.release()
        self.assertFalse(second.is_held_elsewhere())

    def test_lock_excludes_another_process(self):
        path = self.tmp / "x.lock"
        script = textwrap.dedent(
            f"""
            from pathlib import Path
            from wmbot.session import ProcessLock, AlreadyRunning
            try:
                ProcessLock(Path({str(path)!r})).acquire()
            except AlreadyRunning:
                raise SystemExit(3)
            """
        )
        with ProcessLock(path):
            env = {**os.environ, "PYTHONPATH": "src"}
            self.assertEqual(subprocess.run([sys.executable, "-c", script], env=env).returncode, 3)


class RefreshTests(ServerTestCase):
    def auth(self, current=None):
        self.sleeps = []
        store = SessionStore(self.config.session_file)
        return Authenticator(self.config, store, current or session(), sleep=self.sleeps.append), store

    def test_rotation_is_persisted_and_old_token_never_resent(self):
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens(1)), Reply(200, fresh_tokens(2)))
        auth, store = self.auth()
        auth.refresh()
        self.assertEqual(store.load().refresh_token, "fake-refresh-1")
        auth.refresh()
        self.assertEqual(store.load().refresh_token, "fake-refresh-2")
        sent = [json.loads(c["body"])["refresh_token"] for c in self.mock.hits(REFRESH_PATH)]
        self.assertEqual(sent, [FAKE_REFRESH, "fake-refresh-1"])

    def test_token_goes_in_body_not_url(self):
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens()))
        auth, _ = self.auth()
        auth.refresh()
        call = self.mock.hits(REFRESH_PATH)[0]
        self.assertEqual(call["headers"]["Apikey"], "fake-anon")
        self.assertIn(FAKE_REFRESH.encode(), call["body"])

    def test_persisted_file_has_only_three_fields(self):
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens()))
        auth, _ = self.auth()
        auth.refresh()
        self.assertEqual(
            set(json.loads(self.config.session_file.read_text())), {"access_token", "refresh_token", "expires_at"}
        )

    def test_invalid_grant_is_fatal_and_session_untouched(self):
        self.mock.on(REFRESH_PATH, Reply(400, {"error_code": "refresh_token_already_used"}))
        auth, store = self.auth()
        store.save(session(5))
        with self.assertRaisesRegex(FatalAuthError, "refresh_token_already_used"):
            auth.refresh()
        self.assertEqual(store.load().expires_at, 5)
        self.assertEqual(len(self.mock.hits(REFRESH_PATH)), 1)

    def test_server_5xx_is_transient_without_replaying_token(self):
        self.mock.on(REFRESH_PATH, Reply(503, {}))
        auth, store = self.auth()
        with self.assertRaises(TransientAuthError):
            auth.refresh()
        self.assertEqual(len(self.mock.hits(REFRESH_PATH)), 1)
        self.assertEqual(self.sleeps, [])

    def test_lost_response_retries_same_token_inside_reuse_window(self):
        self.mock.on(REFRESH_PATH, Reply(0), Reply(200, fresh_tokens(1)))
        auth, store = self.auth()
        auth.refresh()
        sent = [json.loads(c["body"])["refresh_token"] for c in self.mock.hits(REFRESH_PATH)]
        self.assertEqual(sent, [FAKE_REFRESH, FAKE_REFRESH])
        self.assertEqual(store.load().refresh_token, "fake-refresh-1")

    def test_garbage_200_does_not_overwrite_session(self):
        self.mock.on(REFRESH_PATH, Reply(200, {"nope": 1}))
        auth, store = self.auth()
        store.save(session(5))
        with self.assertRaises(TransientAuthError):
            auth.refresh()
        self.assertEqual(store.load().expires_at, 5)

    def test_ensure_fresh_only_refreshes_inside_margin(self):
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens()))
        auth, _ = self.auth(session(int(time.time()) + 3000))
        auth.ensure_fresh()
        self.assertEqual(self.mock.hits(REFRESH_PATH), [])
        auth, _ = self.auth(session(int(time.time()) + 300))
        auth.ensure_fresh()
        self.assertEqual(len(self.mock.hits(REFRESH_PATH)), 1)

    def test_failed_persist_is_fatal_and_never_uses_unsaved_token(self):
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens(1)))
        auth, store = self.auth()
        with mock.patch.object(store, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(FatalAuthError, "persist"):
                auth.refresh()
        self.assertFalse(self.config.session_file.exists())
        self.assertEqual(len(self.mock.hits(REFRESH_PATH)), 1)


class CookieTests(unittest.TestCase):
    def test_cookie_chunks_at_3180_and_round_trips(self):
        big = Session("a" * 5000, "r" * 50, 99)
        header = build_cookie(big, "ref")
        parts = header.split("; ")
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].startswith("sb-ref-auth-token.0=base64-"))
        self.assertLessEqual(len(parts[0].split("=", 1)[1]), 3180)
        rebuilt = session_from_set_cookies(parts, "ref")
        self.assertEqual(rebuilt, big)

    def test_set_cookie_rotation_parsing(self):
        value = cookie_value(fresh_tokens(9))
        got = session_from_set_cookies(
            [
                f"sb-ref-auth-token.0={value[:20]}; Path=/; HttpOnly",
                f"sb-ref-auth-token.1={value[20:]}; Path=/",
                "other=1; Path=/",
            ],
            "ref",
        )
        self.assertEqual(got.refresh_token, "fake-refresh-9")

    def test_clearing_or_foreign_cookies_are_ignored(self):
        self.assertIsNone(session_from_set_cookies(["sb-ref-auth-token.0=; Max-Age=0"], "ref"))
        self.assertIsNone(session_from_set_cookies(["sb-other-auth-token.0=abc"], "ref"))
        self.assertIsNone(session_from_set_cookies(["sb-ref-auth-token=base64-!!!"], "ref"))


class AnonDiscoveryTests(ServerTestCase):
    def jwt(self, role, ref):
        payload = base64.urlsafe_b64encode(json.dumps({"role": role, "ref": ref}).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{payload}.signaturesignature"

    def test_picks_anon_key_of_our_project(self):
        self.mock.on("/login", Reply(200, b'<script src="/_next/static/a.js?x=1"></script>'))
        chunk = f'x="{self.jwt("service_role", "testref")}";y="{self.jwt("anon", "other")}";z="{self.jwt("anon", "testref")}"'
        self.mock.on("/_next/static/a.js", Reply(200, chunk.encode()))
        self.assertEqual(discover_anon_key(self.config), self.jwt("anon", "testref"))

    def test_no_key_raises(self):
        self.mock.on("/login", Reply(200, b"<html></html>"))
        with self.assertRaises(NetworkError):
            discover_anon_key(self.config)
