from __future__ import annotations

import http.client
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

JWT = re.compile(r"eyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]*")
SECRET_FIELD = re.compile(r'("?(?:access_token|refresh_token|provider_token)"?\s*[:=]\s*"?)[^",;\s]+')
SB_COOKIE = re.compile(r"(sb-[a-z0-9]+-auth-token(?:\.\d+)?=)[^;\s]+")
WEBHOOK = re.compile(r"(/api/webhooks/)\S+")


def redact(text: str) -> str:
    text = SB_COOKIE.sub(r"\1[redacted]", text)
    text = SECRET_FIELD.sub(r"\1[redacted]", text)
    text = WEBHOOK.sub(r"\1[redacted]", text)
    return JWT.sub("[redacted-jwt]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class NetworkError(Exception):
    pass


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    set_cookies: list[str] = field(default_factory=list)

    def json(self) -> object | None:
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status < 400


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _wrap(raw: http.client.HTTPResponse | urllib.error.HTTPError, limit: int) -> Response:
    message = raw.headers
    return Response(
        status=raw.status or 0,
        body=raw.read(limit),
        headers={k.lower(): v for k, v in message.items()},
        set_cookies=message.get_all("Set-Cookie") or [],
    )


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30,
    limit: int = 5_000_000,
) -> Response:
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as raw:
            return _wrap(raw, limit)
    except urllib.error.HTTPError as err:
        with err:
            return _wrap(err, limit)
    except (OSError, http.client.HTTPException, ValueError) as err:
        reason = getattr(err, "reason", err)
        raise NetworkError(redact(str(reason))) from (reason if isinstance(reason, Exception) else None)
