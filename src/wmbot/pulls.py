from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger("wmbot")

WIKIPEDIA_BASE = "https://fr.wikipedia.org/wiki/"


@dataclass(frozen=True)
class Card:
    title: str
    rarity: str
    atk: int | None = None
    defense: int | None = None
    id: str | None = None
    url: str | None = None
    image: str | None = None
    category: str | None = None
    q_score: float | None = None
    pageviews: int | None = None
    owned: int | None = None

    @property
    def key(self) -> str:
        return self.id or self.title

    @property
    def link(self) -> str:
        return self.url or WIKIPEDIA_BASE + urllib.parse.quote(self.title.replace(" ", "_"))

    @property
    def power(self) -> int:
        return (self.atk or 0) + (self.defense or 0)


@dataclass(frozen=True)
class Pull:
    at: dt.datetime
    cards: tuple[Card, ...]
    packs_remaining: int | None = None


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def owned_count(card: dict[str, object], owned: object) -> int | None:
    per_card = _number(card.get("owned_copies"))
    if per_card is not None:
        return per_card
    card_id = str(card.get("id"))
    if isinstance(owned, dict):
        return _number(owned.get(card_id))
    if isinstance(owned, list):
        for entry in owned:
            if isinstance(entry, dict) and str(entry.get("card_id", entry.get("id"))) == card_id:
                for key in ("copies", "count", "owned_copies", "quantity"):
                    found = _number(entry.get(key))
                    if found is not None:
                        return found
    return None


def card_from_api(raw: dict[str, object], owned: object = None) -> Card:
    q_score = raw.get("q_score")
    return Card(
        title=_text(raw.get("wikipedia_title")) or "?",
        rarity=_text(raw.get("rarity")) or "?",
        atk=_number(raw.get("atk")),
        defense=_number(raw.get("def")),
        id=str(raw["id"]) if raw.get("id") is not None else None,
        url=_text(raw.get("wikipedia_url")),
        image=_text(raw.get("image_url")),
        category=_text(raw.get("category")),
        q_score=q_score if isinstance(q_score, (int, float)) else None,
        pageviews=_number(raw.get("pageviews")),
        owned=owned_count(raw, owned),
    )


def _card_from_log(raw: dict[str, object]) -> Card:
    if "r" in raw or "t" in raw:
        return Card(
            title=_text(raw.get("t")) or "?",
            rarity=_text(raw.get("r")) or "?",
            atk=_number(raw.get("atk")),
            defense=_number(raw.get("def")),
        )
    fields = Card.__dataclass_fields__
    return Card(**{key: value for key, value in raw.items() if key in fields})


def append_pull(path: Path, pull: Pull) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cards = [{k: v for k, v in asdict(card).items() if v is not None} for card in pull.cards]
    line = {"at": pull.at.isoformat(), "cards": cards}
    if pull.packs_remaining is not None:
        line["packs_remaining"] = pull.packs_remaining
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def read_pulls(path: Path) -> Iterator[Pull]:
    if not path.exists():
        return
    with open(path, encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                at = dt.datetime.fromisoformat(raw["at"].replace("Z", "+00:00"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=dt.timezone.utc)
                cards = tuple(_card_from_log(card) for card in raw["cards"])
                remaining = raw.get("packs_remaining")
            except (ValueError, KeyError, TypeError, AttributeError):
                log.warning("skipping malformed line %d in %s", number, path.name)
                continue
            yield Pull(at, cards, remaining if isinstance(remaining, int) else None)
