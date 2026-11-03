from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

from wmbot.config import Config
from wmbot.net import Response, request
from wmbot.pulls import Card, card_from_api
from wmbot.session import Session, build_cookie

DEFAULT_RATE_WAIT = 120.0
DEFAULT_DAILY_WAIT = 3600.0
MIN_RATE_WAIT = 15.0
MAX_RATE_WAIT = 26 * 3600.0
VERIFICATION_MARKER = b"human_verification_required"


class Status(Enum):
    OPENED = "opened"
    VERIFICATION_REQUIRED = "human_verification_required"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    SERVER_ERROR = "server_error"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class OpenResult:
    status: Status
    response: Response
    cards: tuple[Card, ...] = ()
    packs_remaining: int | None = None
    last_regen_at: str | None = None
    wait: float | None = None
    daily: bool = False


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def seconds_until(value: object, now: dt.datetime) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return (moment - now).total_seconds()


def rate_limit_wait(data: dict[str, object], response: Response, now: dt.datetime) -> float:
    daily = bool(data.get("rate_limit_daily"))
    wait = seconds_until(data.get("retry_after"), now)
    if wait is None:
        wait = seconds_until(response.headers.get("retry-after"), now)
    if wait is None:
        wait = DEFAULT_DAILY_WAIT if daily else DEFAULT_RATE_WAIT
    return min(max(wait, MIN_RATE_WAIT), MAX_RATE_WAIT)


def classify_open(response: Response, ref_now: dt.datetime) -> OpenResult:
    parsed = response.json()
    data: dict[str, object] = parsed if isinstance(parsed, dict) else {}
    if VERIFICATION_MARKER in response.body and not (response.status == 200 and data.get("cards")):
        return OpenResult(Status.VERIFICATION_REQUIRED, response)
    if response.status == 429 or data.get("rate_limited"):
        remaining = data.get("packs_remaining")
        return OpenResult(
            Status.RATE_LIMITED,
            response,
            packs_remaining=remaining if isinstance(remaining, int) else None,
            wait=rate_limit_wait(data, response, ref_now),
            daily=bool(data.get("rate_limit_daily")),
        )
    if response.status == 401 or (response.is_redirect and "/login" in response.headers.get("location", "")):
        return OpenResult(Status.UNAUTHORIZED, response)
    if response.status == 403:
        return OpenResult(Status.FORBIDDEN, response)
    if response.status != 200:
        return OpenResult(Status.SERVER_ERROR, response)
    raw_cards = data.get("cards")
    if not isinstance(raw_cards, list) or not raw_cards or not all(isinstance(c, dict) for c in raw_cards):
        return OpenResult(Status.MALFORMED, response)
    remaining = data.get("packs_remaining")
    regen = data.get("packs_last_regen_at")
    return OpenResult(
        Status.OPENED,
        response,
        cards=tuple(card_from_api(card, data.get("owned_copies")) for card in raw_cards),
        packs_remaining=remaining if isinstance(remaining, int) else None,
        last_regen_at=regen if isinstance(regen, str) else None,
    )


def open_pack(config: Config, session: Session, now: dt.datetime | None = None) -> OpenResult:
    response = request(
        "POST",
        f"{config.site_url}/api/packs/open",
        headers={
            "Cookie": build_cookie(session, config.supabase_ref),
            "User-Agent": config.user_agent,
            "Origin": config.site_url,
            "Referer": f"{config.site_url}/pulls",
        },
    )
    return classify_open(response, now or utcnow())
