import datetime as dt
import json
import shutil
from pathlib import Path

from tests.support import ServerTestCase, Reply
from wmbot.pulls import Card, Pull, append_pull, read_pulls
from wmbot.report import escape, render_markdown, safe_url, summarize, write_daily_report
from dataclasses import replace

FIXTURE = Path(__file__).parent / "fixtures" / "pulls_legacy.jsonl"
UTC = dt.timezone.utc
DAY = dt.date(2026, 11, 3)


class LegacyFixtureTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.config.data_dir.mkdir()
        shutil.copy(FIXTURE, self.config.pulls_file)

    def test_old_log_format_is_summarized(self):
        summary = summarize(self.config.pulls_file, DAY, UTC)
        self.assertEqual(summary.packs, 2)
        self.assertEqual(summary.cards, 10)
        self.assertEqual(summary.rarities, {"C": 8, "PC": 1, "R": 1})
        self.assertEqual(summary.new, 10)
        self.assertEqual(summary.best[0].title, "Bimétallisme")

    def test_report_links_wikipedia_for_old_lines(self):
        text = render_markdown(summarize(self.config.pulls_file, DAY, UTC))
        self.assertIn("https://fr.wikipedia.org/wiki/Bim%C3%A9tallisme", text)
        self.assertIn("**2** packs", text)
        self.assertIn("| Rare (R) | 1 |", text)

    def test_write_daily_report_path(self):
        path = write_daily_report(self.config, DAY, tz=UTC)
        self.assertEqual(path, self.config.reports_dir / "2026-11-03.md")
        self.assertIn("Best cards", path.read_text())


class SummaryTests(ServerTestCase):
    def pull(self, day, *cards):
        append_pull(self.config.pulls_file, Pull(dt.datetime(2026, 11, day, 12, tzinfo=UTC), tuple(cards)))

    def test_duplicates_compare_with_all_earlier_days_only(self):
        a, b = Card("A", "C", id="1"), Card("B", "UR", atk=1, defense=1, id="2")
        self.pull(2, a)
        self.pull(3, a, b, b)
        self.pull(4, Card("C", "L"))
        summary = summarize(self.config.pulls_file, DAY, UTC)
        self.assertEqual((summary.new, summary.duplicates), (1, 2))
        self.assertEqual(summary.best[0].title, "B")
        self.assertEqual(summary.packs, 1)

    def test_legacy_title_matches_new_card_with_id(self):
        self.pull(2, Card("A", "C"))
        self.pull(3, Card("A", "C", id="1"))
        self.assertEqual(summarize(self.config.pulls_file, DAY, UTC).duplicates, 1)

    def test_rarity_order_beats_power(self):
        self.pull(3, Card("weak", "L", atk=1, defense=1), Card("strong", "C", atk=9000, defense=9000))
        self.assertEqual(summarize(self.config.pulls_file, DAY, UTC).best[0].title, "weak")

    def test_day_boundary_uses_given_timezone(self):
        late = dt.datetime(2026, 11, 3, 23, 30, tzinfo=UTC)
        append_pull(self.config.pulls_file, Pull(late, (Card("A", "C"),)))
        paris = dt.timezone(dt.timedelta(hours=1))
        self.assertEqual(summarize(self.config.pulls_file, DAY, paris).packs, 0)
        self.assertEqual(summarize(self.config.pulls_file, dt.date(2026, 11, 4), paris).packs, 1)

    def test_corrupt_lines_are_skipped(self):
        self.pull(3, Card("A", "C"))
        with open(self.config.pulls_file, "a") as handle:
            handle.write('{"at": "2026-11-03T1\n[]\n{"at": 3, "cards": []}\n\n')
        with self.assertLogs("wmbot", "WARNING"):
            self.assertEqual(len(list(read_pulls(self.config.pulls_file))), 1)

    def test_empty_day_report(self):
        self.assertIn("Nothing opened", render_markdown(summarize(self.config.pulls_file, DAY, UTC)))

    def test_unknown_rarity_is_shown(self):
        self.pull(3, Card("A", "XX"))
        self.assertIn("XX", render_markdown(summarize(self.config.pulls_file, DAY, UTC)))

    def test_markdown_and_html_in_titles_are_neutralized(self):
        evil = Card("x](http://evil) <img src=x> @everyone", "C", url="javascript:alert(1)", image="http://evil/i.png")
        self.pull(3, evil)
        text = render_markdown(summarize(self.config.pulls_file, DAY, UTC))
        self.assertNotIn("<img src=x>", text)
        self.assertNotIn("javascript:", text)
        self.assertNotIn("http://evil/i.png", text)
        self.assertNotIn("@everyone", text)

    def test_escape_and_safe_url(self):
        self.assertEqual(escape("a_b"), "a\\_b")
        self.assertIsNone(safe_url("http://x"))
        self.assertIsNone(safe_url('https://x/a b'))
        self.assertEqual(safe_url("https://x/a"), "https://x/a")


class DiscordTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, discord_webhook=f"{self.mock.url}/hook")
        append_pull(
            self.config.pulls_file,
            Pull(dt.datetime(2026, 11, 3, 12, tzinfo=UTC), (Card("A [x]", "UR", atk=1, defense=2, image="https://i/a.png"),)),
        )

    def test_report_posts_embed_without_mentions(self):
        self.mock.on("/hook", Reply(204, b""))
        write_daily_report(self.config, DAY, tz=UTC)
        body = json.loads(self.mock.hits("/hook")[0]["body"])
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertIn("1 packs", body["embeds"][0]["description"])
        self.assertEqual(body["embeds"][0]["thumbnail"]["url"], "https://i/a.png")
        self.assertIn("DiscordBot", self.mock.hits("/hook")[0]["headers"]["User-Agent"])

    def test_discord_failure_still_leaves_report_on_disk(self):
        from wmbot.net import NetworkError

        self.mock.on("/hook", Reply(500, b""))
        with self.assertRaises(NetworkError):
            write_daily_report(self.config, DAY, tz=UTC)
        self.assertTrue((self.config.reports_dir / "2026-11-03.md").exists())

    def test_skip_existing_does_not_repost(self):
        self.mock.on("/hook", Reply(204, b""))
        write_daily_report(self.config, DAY, tz=UTC)
        self.assertIsNone(write_daily_report(self.config, DAY, skip_existing=True, tz=UTC))
        self.assertEqual(len(self.mock.hits("/hook")), 1)
