from __future__ import annotations

import base64
import datetime as dt
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wmbot.config import Config
from wmbot.session import Session

NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc)
FAKE_ACCESS = "fake.access.token"
FAKE_REFRESH = "fake-refresh-0"


def card(title: str = "Bimétallisme", rarity: str = "R", card_id: int = 1, **extra: object) -> dict[str, object]:
    return {
        "id": card_id,
        "wikipedia_title": title,
        "wikipedia_url": f"https://fr.wikipedia.org/wiki/{title.replace(' ', '_')}",
        "image_url": f"https://img.example/{card_id}.jpg",
        "category": "economy",
        "q_score": 0.5,
        "rarity": rarity,
        "atk": 1000,
        "def": 2000,
        "pageviews": 42,
        **extra,
    }


def pack(remaining: int = 3, regen: str | None = None, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "cards": [card(f"Card {i}", card_id=i) for i in range(5)],
        "packs_remaining": remaining,
        "packs_last_regen_at": regen,
        "owned_copies": {},
    }
    return {**body, **extra}


class Reply:
    def __init__(
        self,
        status: int = 200,
        body: object = None,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.cookies = cookies or []


class MockServer:
    def __init__(self) -> None:
        self.routes: dict[str, list[Reply]] = {}
        self.calls: list[dict[str, object]] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                mock.calls.append(
                    {"method": self.command, "path": path, "headers": dict(self.headers), "body": raw}
                )
                queue = mock.routes.get(path) or []
                reply = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else Reply(404, {}))
                if reply.status == 0:
                    self.connection.close()
                    return
                payload = reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                for value in reply.cookies:
                    self.send_header("Set-Cookie", value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def on(self, path: str, *replies: Reply) -> None:
        self.routes[path] = list(replies)

    def hits(self, path: str) -> list[dict[str, object]]:
        return [call for call in self.calls if call["path"] == path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


REFRESH_PATH = "/auth/v1/token"
OPEN_PATH = "/api/packs/open"


def fresh_tokens(n: int = 1, expires_at: int = 4_000_000_000) -> dict[str, object]:
    return {
        "access_token": f"fake.access.{n}",
        "refresh_token": f"fake-refresh-{n}",
        "expires_at": expires_at,
        "user": {"email": "should-be-dropped@example.com"},
    }


def session(expires_at: int = 4_000_000_000) -> Session:
    return Session(FAKE_ACCESS, FAKE_REFRESH, expires_at)


def cookie_value(tokens: dict[str, object]) -> str:
    return "base64-" + base64.urlsafe_b64encode(json.dumps(tokens).encode()).decode().rstrip("=")


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockServer()
        self.addCleanup(self.mock.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.config = Config(
            session_file=self.tmp / "secrets" / "session.json",
            data_dir=self.tmp / "data",
            site_url=self.mock.url,
            supabase_url=self.mock.url,
            supabase_ref="testref",
            anon_key="fake-anon",
        )
