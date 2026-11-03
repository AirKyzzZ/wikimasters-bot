from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from wmbot.config import Config
from wmbot.net import NetworkError, request
from wmbot.pulls import Card, read_pulls

RARITY_ORDER = ("L", "UR", "SR", "R", "PC", "C")
RARITY_NAMES = {
    "L": "Legendary",
    "UR": "Ultra rare",
    "SR": "Super rare",
    "R": "Rare",
    "PC": "Uncommon",
    "C": "Common",
}
BEST_CARDS = 5
MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#|!~])")


def escape(text: str) -> str:
    return MARKDOWN_SPECIAL.sub(r"\\\1", text.replace("\n", " ")).replace("@", "@​")


def safe_url(url: str | None) -> str | None:
    if not url or urlsplit(url).scheme != "https" or re.search(r"[\s()<>]", url):
        return None
    return url


def rarity_rank(rarity: str) -> int:
    return RARITY_ORDER.index(rarity) if rarity in RARITY_ORDER else len(RARITY_ORDER)


@dataclass(frozen=True)
class DailySummary:
    day: dt.date
    packs: int
    rarities: Counter[str]
    new: int
    duplicates: int
    best: tuple[Card, ...]

    @property
    def cards(self) -> int:
        return sum(self.rarities.values())


def summarize(path: Path, day: dt.date, tz: dt.tzinfo | None = None) -> DailySummary:
    seen: set[str] = set()
    rarities: Counter[str] = Counter()
    today: list[Card] = []
    packs = new = duplicates = 0
    for pull in read_pulls(path):
        local_day = pull.at.astimezone(tz).date()
        if local_day > day:
            continue
        in_day = local_day == day
        packs += in_day
        for card in pull.cards:
            first_time = card.key not in seen and card.title not in seen
            seen.update({card.key, card.title})
            if not in_day:
                continue
            today.append(card)
            rarities[card.rarity] += 1
            if first_time:
                new += 1
            else:
                duplicates += 1
    best = sorted(today, key=lambda card: (rarity_rank(card.rarity), -card.power))[:BEST_CARDS]
    return DailySummary(day, packs, rarities, new, duplicates, tuple(best))


def _rarity_rows(summary: DailySummary) -> list[tuple[str, int]]:
    known = [(rarity, summary.rarities[rarity]) for rarity in RARITY_ORDER if summary.rarities[rarity]]
    other = [(rarity, count) for rarity, count in summary.rarities.items() if rarity not in RARITY_ORDER]
    return known + sorted(other)


def render_markdown(summary: DailySummary) -> str:
    lines = [
        f"# WikiMasters report, {summary.day.isoformat()}",
        "",
        f"**{summary.packs}** packs opened, **{summary.cards}** cards: "
        f"**{summary.new}** new, **{summary.duplicates}** duplicates.",
        "",
    ]
    if not summary.packs:
        lines.append("Nothing opened that day.")
        return "\n".join(lines) + "\n"
    lines += ["| Rarity | Cards |", "| --- | --- |"]
    lines += [
        f"| {escape(RARITY_NAMES.get(rarity, rarity))} ({escape(rarity)}) | {count} |"
        for rarity, count in _rarity_rows(summary)
    ]
    lines += ["", "## Best cards", ""]
    for card in summary.best:
        stats = f"ATK {card.atk} / DEF {card.defense}" if card.atk is not None else ""
        title = escape(card.title)
        link = safe_url(card.link)
        named = f"[{title}]({link})" if link else title
        lines.append(f"- **{escape(card.rarity)}** {named} {stats}".rstrip())
        image = safe_url(card.image)
        if image:
            lines.append(f'  <br><img src="{image}" alt="" width="160">')
    return "\n".join(lines) + "\n"


def write_daily_report(
    config: Config,
    day: dt.date,
    *,
    skip_existing: bool = False,
    tz: dt.tzinfo | None = None,
) -> Path | None:
    target = config.reports_dir / f"{day.isoformat()}.md"
    if skip_existing and target.exists():
        return None
    summary = summarize(config.pulls_file, day, tz)
    if skip_existing and not summary.packs:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(render_markdown(summary), encoding="utf-8")
    tmp.replace(target)
    if config.discord_webhook:
        try:
            post_discord(config, summary)
        except NetworkError as err:
            raise NetworkError(f"report written, discord failed: {err}") from None
    return target


def _discord_post(config: Config, payload: dict[str, object]) -> None:
    response = request(
        "POST",
        config.discord_webhook,
        data=json.dumps({**payload, "allowed_mentions": {"parse": []}}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (wmbot, 0.1)"},
        timeout=15,
    )
    if response.status >= 300:
        raise NetworkError(f"discord answered {response.status}")


def notify_discord(config: Config, message: str) -> None:
    _discord_post(config, {"content": message[:1900]})


def post_discord(config: Config, summary: DailySummary) -> None:
    rarity_line = " · ".join(f"{rarity} {count}" for rarity, count in _rarity_rows(summary))
    cards = []
    for card in summary.best:
        link = safe_url(card.link)
        title = card.title.replace("[", "(").replace("]", ")")
        cards.append(f"**{card.rarity}** [{title}]({link})" if link else f"**{card.rarity}** {title}")
    embed: dict[str, object] = {
        "title": f"WikiMasters, {summary.day.isoformat()}",
        "description": "\n".join(
            [
                f"{summary.packs} packs, {summary.cards} cards, "
                f"{summary.new} new, {summary.duplicates} duplicates",
                rarity_line,
                "",
                *cards,
            ]
        )[:4000],
    }
    image = safe_url(summary.best[0].image) if summary.best else None
    if image:
        embed["thumbnail"] = {"url": image}
    _discord_post(config, {"embeds": [embed]})
