import datetime as dt
import json
import random
import threading
import time
from dataclasses import replace
from unittest import mock

from tests.support import (
    FAKE_ACCESS,
    NOW,
    OPEN_PATH,
    REFRESH_PATH,
    Reply,
    ServerTestCase,
    card,
    cookie_value,
    fresh_tokens,
    pack,
    session,
)
from wmbot.client import MAX_RATE_WAIT, MIN_RATE_WAIT, seconds_until
from wmbot.pulls import Card, Pull, append_pull, read_pulls
from wmbot.runner import Bot, ForbiddenError, regen_wait
from wmbot.session import Authenticator, FatalAuthError, SessionStore


def iso(seconds: float) -> str:
    return (NOW + dt.timedelta(seconds=seconds)).isoformat()


class RegenTimingTests(ServerTestCase):
    def test_wait_counts_from_last_regen_not_from_now(self):
        self.assertEqual(regen_wait(iso(-240), NOW), 360)

    def test_just_regenerated_waits_full_period(self):
        self.assertEqual(regen_wait(iso(0), NOW), 600)

    def test_overdue_regen_uses_floor(self):
        self.assertEqual(regen_wait(iso(-5000), NOW), 30)

    def test_future_timestamp_is_capped(self):
        self.assertEqual(regen_wait(iso(9000), NOW), 660)

    def test_missing_or_garbage_timestamp_waits_full_period(self):
        self.assertEqual(regen_wait(None, NOW), 600)
        self.assertEqual(regen_wait("not-a-date", NOW), 600)

    def test_seconds_until_accepts_iso_z_numbers_and_rejects_garbage(self):
        self.assertEqual(seconds_until("2026-11-03T12:01:00Z", NOW), 60)
        self.assertEqual(seconds_until(30, NOW), 30)
        self.assertEqual(seconds_until("45", NOW), 45)
        self.assertIsNone(seconds_until("soon", NOW))
        self.assertIsNone(seconds_until(True, NOW))
        self.assertIsNone(seconds_until(None, NOW))


class BotTests(ServerTestCase):
    def bot(self, current=None):
        store = SessionStore(self.config.session_file)
        self.store = store
        store.save(current or session())
        auth = Authenticator(self.config, store, current or session(), sleep=lambda _: None)
        return Bot(self.config, auth, rng=random.Random(1), clock=lambda: NOW)

    def test_open_with_stock_waits_20_to_40s_and_logs_pull(self):
        self.mock.on(OPEN_PATH, Reply(200, pack(remaining=3)))
        wait, outcome = self.bot().step()
        self.assertEqual(outcome, "opened")
        self.assertTrue(20 <= wait <= 40)
        pulls = list(read_pulls(self.config.pulls_file))
        self.assertEqual(len(pulls), 1)
        self.assertEqual(len(pulls[0].cards), 5)
        self.assertEqual(pulls[0].cards[0].image, "https://img.example/0.jpg")
        self.assertEqual(pulls[0].packs_remaining, 3)

    def test_open_sends_session_cookie_and_origin(self):
        self.mock.on(OPEN_PATH, Reply(200, pack()))
        self.bot().step()
        call = self.mock.hits(OPEN_PATH)[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["headers"]["Cookie"].startswith("sb-testref-auth-token.0=base64-"))
        self.assertEqual(call["headers"]["Origin"], self.config.site_url)
        self.assertIn("/pulls", call["headers"]["Referer"])

    def test_empty_stock_waits_for_next_regen_with_jitter(self):
        self.mock.on(OPEN_PATH, Reply(200, pack(remaining=0, regen=iso(-240))))
        wait, _ = self.bot().step()
        self.assertTrue(365 <= wait <= 390, wait)

    def test_429_normal_respects_retry_after(self):
        self.mock.on(OPEN_PATH, Reply(429, {"rate_limited": True, "retry_after": iso(300), "packs_remaining": 0}))
        bot = self.bot()
        wait, outcome = bot.step()
        self.assertEqual(outcome, "rate_limited")
        self.assertTrue(303 <= wait <= 315, wait)
        self.assertEqual(bot.packs_remaining, 0)
        self.assertEqual(list(read_pulls(self.config.pulls_file)), [])

    def test_429_daily_sleeps_until_reset(self):
        self.mock.on(
            OPEN_PATH,
            Reply(429, {"rate_limited": True, "rate_limit_daily": True, "retry_after": iso(30000), "packs_remaining": 2}),
        )
        wait, outcome = self.bot().step()
        self.assertEqual(outcome, "daily_limit")
        self.assertTrue(30003 <= wait <= 30015, wait)

    def test_429_daily_without_retry_after_waits_an_hour(self):
        self.mock.on(OPEN_PATH, Reply(429, {"rate_limited": True, "rate_limit_daily": True}))
        wait, _ = self.bot().step()
        self.assertGreaterEqual(wait, 3600)

    def test_429_uses_retry_after_header_when_body_is_not_json(self):
        self.mock.on(OPEN_PATH, Reply(429, b"slow down", headers={"Retry-After": "90"}))
        wait, _ = self.bot().step()
        self.assertTrue(93 <= wait <= 105, wait)

    def test_429_garbage_or_past_retry_after_never_hammers_or_crashes(self):
        self.mock.on(OPEN_PATH, Reply(429, {"retry_after": {"weird": 1}}), Reply(429, {"retry_after": iso(-999)}))
        bot = self.bot()
        self.assertGreaterEqual(bot.step()[0], 120)
        self.assertGreaterEqual(bot.step()[0], MIN_RATE_WAIT)

    def test_429_absurd_retry_after_is_capped(self):
        self.mock.on(OPEN_PATH, Reply(429, {"retry_after": iso(10**8)}))
        self.assertLessEqual(self.bot().step()[0], MAX_RATE_WAIT + 15)

    def test_401_refreshes_then_succeeds_with_new_token(self):
        self.mock.on(OPEN_PATH, Reply(401, {}), Reply(200, pack()))
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens(1)))
        bot = self.bot()
        wait, outcome = bot.step()
        self.assertEqual(outcome, "unauthorized")
        self.assertTrue(5 <= wait <= 15)
        self.assertEqual(self.store.load().refresh_token, "fake-refresh-1")
        bot.step()
        self.assertEqual(bot.auth_failures, 0)
        self.assertNotEqual(self.mock.hits(OPEN_PATH)[0]["headers"]["Cookie"], self.mock.hits(OPEN_PATH)[1]["headers"]["Cookie"])

    def test_repeated_401_is_fatal_on_third(self):
        self.mock.on(OPEN_PATH, Reply(401, {}))
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens(1)))
        bot = self.bot()
        bot.step()
        bot.step()
        with self.assertRaises(FatalAuthError):
            bot.step()
        self.assertEqual(len(self.mock.hits(REFRESH_PATH)), 2)

    def test_401_with_rejected_refresh_is_fatal_not_a_crash_loop(self):
        self.mock.on(OPEN_PATH, Reply(401, {}))
        self.mock.on(REFRESH_PATH, Reply(400, {"error_code": "refresh_token_not_found"}))
        with self.assertRaises(FatalAuthError):
            self.bot().step()

    def test_401_with_refresh_network_failure_backs_off(self):
        self.mock.on(OPEN_PATH, Reply(401, {}))
        self.mock.on(REFRESH_PATH, Reply(0))
        wait, outcome = self.bot().step()
        self.assertEqual(outcome, "refresh_failed")
        self.assertGreaterEqual(wait, 60)

    def test_redirect_to_login_counts_as_401_and_is_not_followed(self):
        self.mock.on(OPEN_PATH, Reply(307, b"", headers={"Location": "/login"}))
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens()))
        _, outcome = self.bot().step()
        self.assertEqual(outcome, "unauthorized")
        self.assertEqual(self.mock.hits("/login"), [])

    def test_redirect_elsewhere_does_not_leak_cookie(self):
        self.mock.on(OPEN_PATH, Reply(302, b"", headers={"Location": "/elsewhere"}))
        _, outcome = self.bot().step()
        self.assertEqual(outcome, "server_error")
        self.assertEqual(self.mock.hits("/elsewhere"), [])

    def test_403_never_refreshes_and_stops_on_third(self):
        self.mock.on(OPEN_PATH, Reply(403, {}))
        bot = self.bot()
        self.assertGreaterEqual(bot.step()[0], 1800)
        self.assertGreaterEqual(bot.step()[0], 3600)
        with self.assertRaises(ForbiddenError):
            bot.step()
        self.assertEqual(self.mock.hits(REFRESH_PATH), [])

    def test_verification_backs_off_and_never_touches_session(self):
        self.mock.on(OPEN_PATH, Reply(403, {"error": "human_verification_required"}))
        bot = self.bot()
        before = self.config.session_file.read_bytes()
        wait, outcome = bot.step()
        self.assertEqual(outcome, "verification_required")
        self.assertTrue(1800 <= wait <= 2250, wait)
        self.assertEqual(self.mock.hits(REFRESH_PATH), [])
        self.assertEqual(bot.verification_hits, 1)
        self.assertEqual(self.config.session_file.read_bytes(), before)
        self.assertEqual(list(read_pulls(self.config.pulls_file)), [])
        second = bot.step()[0]
        self.assertGreater(second, wait)

    def test_verification_delay_resets_after_a_successful_open(self):
        self.mock.on(
            OPEN_PATH,
            Reply(403, {"error": "human_verification_required"}),
            Reply(403, {"error": "human_verification_required"}),
            Reply(200, pack()),
        )
        bot = self.bot()
        bot.step()
        first_try_then = bot.step()[0]
        wait, outcome = bot.step()
        self.assertEqual(outcome, "opened")
        self.assertTrue(20 <= wait <= 40, wait)
        self.assertEqual(bot.verification_hits, 0)
        self.assertEqual(bot.errors, 0)

    def test_verification_detected_on_any_status_and_shape(self):
        replies = [
            Reply(429, {"code": "human_verification_required", "retry_after": iso(5)}),
            Reply(401, {"detail": {"reason": "human_verification_required"}}),
            Reply(200, {"error": "human_verification_required"}),
            Reply(403, b"<html>human_verification_required</html>"),
            Reply(403, {"error": "Vérification anti-bot requise pour continuer à ouvrir des paquets."}),
            Reply(200, {"error": "Vérification anti-bot requise"}),
        ]
        for reply in replies:
            with self.subTest(status=reply.status):
                self.mock.on(OPEN_PATH, reply)
                self.assertEqual(self.bot().step()[1], "verification_required")

    def test_card_mentioning_the_marker_is_still_a_pack(self):
        self.mock.on(OPEN_PATH, Reply(200, pack(cards=[card("human_verification_required")])))
        self.assertEqual(self.bot().step()[1], "opened")

    def test_plain_403_is_not_treated_as_verification(self):
        self.mock.on(OPEN_PATH, Reply(403, {"error": "forbidden"}))
        self.assertEqual(self.bot().step()[1], "forbidden")

    def test_5xx_backs_off_exponentially_up_to_cap_and_resets(self):
        self.mock.on(OPEN_PATH, *[Reply(503, b"x") for _ in range(9)], Reply(200, pack()))
        bot = self.bot()
        waits = [bot.step()[0] for _ in range(9)]
        self.assertTrue(60 <= waits[0] <= 75)
        self.assertTrue(120 <= waits[1] <= 150)
        self.assertLessEqual(max(waits), 3600 * 1.25)
        self.assertEqual(bot.errors, 9)
        bot.step()
        self.assertEqual(bot.errors, 0)

    def test_network_error_backs_off(self):
        self.mock.on(OPEN_PATH, Reply(0))
        wait, outcome = self.bot().step()
        self.assertEqual(outcome, "network_error")
        self.assertGreaterEqual(wait, 60)

    def test_connection_refused_backs_off(self):
        bot = self.bot()
        bot.config = replace(self.config, site_url="http://127.0.0.1:9")
        self.assertEqual(bot.step()[1], "network_error")

    def test_malformed_200_backs_off_without_logging_a_pull(self):
        self.mock.on(OPEN_PATH, Reply(200, b"<html>"), Reply(200, {"cards": []}), Reply(200, {"cards": ["x"]}))
        bot = self.bot()
        for _ in range(3):
            self.assertEqual(bot.step()[1], "malformed")
        self.assertEqual(list(read_pulls(self.config.pulls_file)), [])

    def test_200_with_rate_limited_flag_is_rate_limit(self):
        self.mock.on(OPEN_PATH, Reply(200, {"rate_limited": True, "retry_after": iso(100)}))
        self.assertEqual(self.bot().step()[1], "rate_limited")

    def test_set_cookie_rotation_is_persisted(self):
        cookie = f"sb-testref-auth-token.0={cookie_value(fresh_tokens(7))}; Path=/"
        self.mock.on(OPEN_PATH, Reply(200, pack(), cookies=[cookie]))
        bot = self.bot()
        bot.step()
        self.assertEqual(self.store.load().refresh_token, "fake-refresh-7")
        self.assertEqual(bot.auth.session.access_token, "fake.access.7")

    def test_unwritable_pull_log_does_not_lose_the_session_or_crash(self):
        self.mock.on(OPEN_PATH, Reply(200, pack()))
        self.config.data_dir.parent.mkdir(exist_ok=True)
        self.config.data_dir.write_text("a file, not a directory")
        with self.assertLogs("wmbot", "ERROR"):
            wait, outcome = self.bot().step()
        self.assertEqual(outcome, "opened")
        self.assertGreater(wait, 0)

    def test_expired_token_refreshes_before_opening(self):
        self.mock.on(OPEN_PATH, Reply(200, pack()))
        self.mock.on(REFRESH_PATH, Reply(200, fresh_tokens(1)))
        self.bot(session(int(time.time()) - 10)).step()
        self.assertEqual(self.mock.calls[0]["path"], REFRESH_PATH)
        self.assertNotIn(FAKE_ACCESS, json.dumps(self.mock.hits(OPEN_PATH)[0]["headers"]))

    def test_stop_event_ends_run_promptly(self):
        self.mock.on(OPEN_PATH, Reply(200, pack(remaining=0)))
        bot = self.bot()
        worker = threading.Thread(target=bot.run)
        worker.start()
        for _ in range(100):
            if self.config.state_file.exists():
                break
            time.sleep(0.02)
        bot.stop.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        state = json.loads(self.config.state_file.read_text())
        self.assertEqual(state["outcome"], "opened")
        self.assertNotIn("token", json.dumps(state))

    def test_rate_limit_never_consumes_token_or_touches_refresh(self):
        self.mock.on(OPEN_PATH, Reply(429, {"retry_after": iso(100)}))
        self.bot().step()
        self.assertEqual(self.mock.hits(REFRESH_PATH), [])

    def test_auto_report_writes_yesterday_once_and_survives_errors(self):
        append_pull(
            self.config.pulls_file,
            Pull(NOW - dt.timedelta(days=1), (Card("A", "C"),)),
        )
        bot = self.bot()
        bot.maybe_report()
        reports = list(self.config.reports_dir.iterdir())
        self.assertEqual(len(reports), 1)
        reports[0].unlink()
        bot.maybe_report()
        self.assertEqual(list(self.config.reports_dir.iterdir()), [])
        bot._reported = None
        with mock.patch("wmbot.runner.write_daily_report", side_effect=RuntimeError("boom")):
            with self.assertLogs("wmbot", "ERROR"):
                bot.maybe_report()

    def test_fatal_alert_posts_to_discord_without_token(self):
        self.mock.on("/hook", Reply(204, b""))
        bot = self.bot()
        bot.config = replace(self.config, discord_webhook=f"{self.mock.url}/hook")
        with self.assertLogs("wmbot", "ERROR"):
            bot.alert("a new refresh token is needed")
        body = self.mock.hits("/hook")[0]["body"].decode()
        self.assertIn("wmbot: stopped", body)
        self.assertNotIn("fake-refresh", body)

    def start(self, bot):
        worker = threading.Thread(target=bot.run)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(bot.stop.set)
        return worker

    def wait_until(self, condition):
        for _ in range(300):
            if condition():
                return
            time.sleep(0.01)
        self.fail("condition not met")

    def test_keep_running_through_verification_and_notifies_once(self):
        self.mock.on(
            OPEN_PATH,
            Reply(403, {"error": "human_verification_required"}),
            Reply(403, {"error": "human_verification_required"}),
            Reply(200, pack(remaining=0)),
        )
        self.mock.on("/hook", Reply(204, b""))
        bot = self.bot()
        bot.config = replace(self.config, discord_webhook=f"{self.mock.url}/hook")

        def state_outcome():
            try:
                return json.loads(self.config.state_file.read_text())["outcome"]
            except (OSError, ValueError, KeyError):
                return None

        with mock.patch("wmbot.runner.VERIFY_BASE", 0.01), mock.patch("wmbot.runner.VERIFY_CAP", 0.01):
            worker = self.start(bot)
            self.wait_until(lambda: state_outcome() == "opened")
            self.assertEqual(len(self.mock.hits(OPEN_PATH)), 3)
            self.assertEqual(self.mock.hits(REFRESH_PATH), [])
            self.assertEqual(len(self.mock.hits("/hook")), 1)
            self.assertIn("human verification", self.mock.hits("/hook")[0]["body"].decode())
            bot.stop.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_verification_recovered_on_its_own_without_user_action(self):
        self.mock.on(
            OPEN_PATH,
            Reply(403, {"error": "human_verification_required"}),
            Reply(403, {"error": "human_verification_required"}),
            Reply(403, {"error": "human_verification_required"}),
            Reply(200, pack()),
        )
        bot = self.bot()

        def state_outcome():
            try:
                return json.loads(self.config.state_file.read_text())["outcome"]
            except (OSError, ValueError, KeyError):
                return None

        with mock.patch("wmbot.runner.VERIFY_BASE", 0.01), mock.patch("wmbot.runner.VERIFY_CAP", 0.01):
            worker = self.start(bot)
            self.wait_until(lambda: state_outcome() == "opened")
            self.assertEqual(len(self.mock.hits(OPEN_PATH)), 4)
            self.assertEqual(bot.verification_hits, 0)
            bot.stop.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_owned_copies_kept_on_card(self):
        body = pack(cards=[card(card_id=11)], owned_copies={"11": 2})
        self.mock.on(OPEN_PATH, Reply(200, body))
        self.bot().step()
        self.assertEqual(next(read_pulls(self.config.pulls_file)).cards[0].owned, 2)
