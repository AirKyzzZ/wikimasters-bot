from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import tempfile
import threading
from pathlib import Path
from typing import Callable

from wmbot.client import OpenResult, Status, open_pack, seconds_until, utcnow
from wmbot.config import Config
from wmbot.net import NetworkError
from wmbot.pulls import Pull, append_pull
from wmbot.report import notify_discord, write_daily_report
from wmbot.session import Authenticator, FatalAuthError, TransientAuthError

log = logging.getLogger("wmbot")

REGEN_SECONDS = 600.0
MIN_REGEN_WAIT = 30.0
MAX_REGEN_WAIT = 660.0
BACKOFF_BASE = 60.0
BACKOFF_CAP = 3600.0
MAX_AUTH_FAILURES = 3
MAX_FORBIDDEN = 3
FORBIDDEN_WAIT = 1800.0
VERIFY_BASE = 1800.0
VERIFY_CAP = 28800.0


class ForbiddenError(Exception):
    pass


def regen_wait(last_regen_at: str | None, now: dt.datetime) -> float:
    elapsed = seconds_until(last_regen_at, now)
    if elapsed is None:
        return REGEN_SECONDS
    return min(max(elapsed + REGEN_SECONDS, MIN_REGEN_WAIT), MAX_REGEN_WAIT)


def write_json_atomic(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Bot:
    def __init__(
        self,
        config: Config,
        auth: Authenticator,
        rng: random.Random | None = None,
        clock: Callable[[], dt.datetime] = utcnow,
        stop: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.auth = auth
        self.rng = rng or random.Random()
        self.clock = clock
        self.stop = stop or threading.Event()
        self.errors = 0
        self.auth_failures = 0
        self.forbidden = 0
        self.verification_hits = 0
        self.verification_notified = False
        self.last_open_at: str | None = None
        self.packs_remaining: int | None = None
        self._reported: dt.date | None = None

    def backoff(self) -> float:
        self.errors += 1
        delay = min(BACKOFF_BASE * 2 ** (self.errors - 1), BACKOFF_CAP)
        return delay * self.rng.uniform(1.0, 1.25)

    def verification_wait(self) -> float:
        delay = min(VERIFY_BASE * 2 ** self.verification_hits, VERIFY_CAP)
        self.verification_hits += 1
        return delay * self.rng.uniform(1.0, 1.25)

    def _record(self, result: OpenResult, now: dt.datetime) -> None:
        pull = Pull(now, result.cards, result.packs_remaining)
        summary = ", ".join(f"{card.rarity} {card.title}" for card in result.cards)
        log.info("opened: %s | left %s", summary, result.packs_remaining)
        self.last_open_at = now.isoformat()
        self.packs_remaining = result.packs_remaining
        try:
            append_pull(self.config.pulls_file, pull)
        except OSError as err:
            log.error("pack opened but not logged (%s): %s", type(err).__name__, summary)

    def handle(self, result: OpenResult, now: dt.datetime) -> tuple[float, str]:
        if result.status is Status.VERIFICATION_REQUIRED:
            log.warning("human verification required, retrying with a growing delay")
            return self.verification_wait(), "verification_required"
        if result.status is Status.OPENED:
            self.errors = self.auth_failures = self.forbidden = 0
            self.verification_hits = 0
            self._record(result, now)
            if result.packs_remaining and result.packs_remaining > 0:
                return self.rng.uniform(20, 40), "opened"
            return regen_wait(result.last_regen_at, now) + self.rng.uniform(5, 30), "opened"
        if result.status is Status.RATE_LIMITED:
            self.errors = self.forbidden = 0
            self.verification_hits = 0
            self.packs_remaining = result.packs_remaining
            wait = (result.wait or 0) + self.rng.uniform(3, 15)
            log.info("%s limit, sleeping %ds", "daily" if result.daily else "rate", wait)
            return wait, "daily_limit" if result.daily else "rate_limited"
        if result.status is Status.UNAUTHORIZED:
            self.auth_failures += 1
            log.warning("unauthorized (%d/%d)", self.auth_failures, MAX_AUTH_FAILURES)
            if self.auth_failures >= MAX_AUTH_FAILURES:
                raise FatalAuthError("auth keeps failing after refresh, a new refresh token is needed")
            self.auth.refresh()
            return self.rng.uniform(5, 15), "unauthorized"
        if result.status is Status.FORBIDDEN:
            self.forbidden += 1
            log.warning("forbidden (%d/%d), not touching the session", self.forbidden, MAX_FORBIDDEN)
            if self.forbidden >= MAX_FORBIDDEN:
                raise ForbiddenError("site keeps answering 403, the account or IP may be blocked")
            return FORBIDDEN_WAIT * self.forbidden * self.rng.uniform(1.0, 1.25), "forbidden"
        log.warning("%s (http %d)", result.status.value, result.response.status)
        return self.backoff(), result.status.value

    def step(self) -> tuple[float, str]:
        try:
            session = self.auth.ensure_fresh()
            result = open_pack(self.config, session, self.clock())
            self.auth.absorb_cookies(result.response)
            return self.handle(result, self.clock())
        except TransientAuthError as err:
            log.warning("%s", err)
            return self.backoff(), "refresh_failed"
        except NetworkError as err:
            log.warning("network error: %s", err)
            return self.backoff(), "network_error"

    def maybe_report(self) -> None:
        if not self.config.auto_report:
            return
        yesterday = self.clock().astimezone().date() - dt.timedelta(days=1)
        if self._reported == yesterday:
            return
        self._reported = yesterday
        try:
            if write_daily_report(self.config, yesterday, skip_existing=True):
                log.info("wrote report for %s", yesterday)
        except Exception as err:
            log.error("daily report failed: %s: %s", type(err).__name__, err)

    def write_state(self, wait: float | None, outcome: str) -> None:
        now = self.clock()
        next_attempt = None if wait is None else (now + dt.timedelta(seconds=wait)).isoformat()
        state: dict[str, object] = {
            "updated_at": now.isoformat(),
            "outcome": outcome,
            "next_attempt_at": next_attempt,
            "last_open_at": self.last_open_at,
            "packs_remaining": self.packs_remaining,
            "consecutive_errors": self.errors,
        }
        try:
            write_json_atomic(self.config.state_file, state)
        except OSError as err:
            log.warning("cannot write state: %s", type(err).__name__)

    def notify(self, message: str) -> None:
        log.error("%s", message)
        if not self.config.discord_webhook:
            return
        try:
            notify_discord(self.config, f"wmbot: {message}")
        except NetworkError as err:
            log.warning("discord alert failed: %s", err)

    def alert(self, message: str) -> None:
        self.notify(f"stopped, {message}")

    def run(self) -> None:
        while not self.stop.is_set():
            self.maybe_report()
            wait, outcome = self.step()
            if outcome == "verification_required":
                if not self.verification_notified:
                    self.verification_notified = True
                    self.notify("the site asks for human verification, the bot keeps trying until it clears")
            else:
                self.verification_notified = False
            self.write_state(wait, outcome)
            self.stop.wait(wait)