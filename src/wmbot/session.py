from __future__ import annotations

import base64
import binascii
import fcntl
import json
import logging
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable

from wmbot.config import Config
from wmbot.net import NetworkError, Response, request

log = logging.getLogger("wmbot")

COOKIE_CHUNK = 3180
REFRESH_MARGIN = 600
REUSE_RETRY_DELAY = 2.0
TERMINAL_AUTH_STATUSES = {400, 401, 422}


class SessionError(Exception):
    pass


class FatalAuthError(SessionError):
    pass


class TransientAuthError(SessionError):
    pass


class AlreadyRunning(Exception):
    pass


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str
    expires_at: int

    def expires_in(self, now: float | None = None) -> float:
        return self.expires_at - (time.time() if now is None else now)

    def __repr__(self) -> str:
        return f"Session(expires_at={self.expires_at})"


def session_from_mapping(raw: object) -> Session:
    if not isinstance(raw, dict) or not isinstance(raw.get("refresh_token"), str) or not raw["refresh_token"]:
        raise SessionError("session has no refresh_token")
    expires_at = raw.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        expires_in = raw.get("expires_in")
        expires_at = time.time() + expires_in if isinstance(expires_in, (int, float)) else 0
    access = raw.get("access_token")
    return Session(access if isinstance(access, str) else "", raw["refresh_token"], int(expires_at))


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Session:
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & 0o077:
                log.warning("session file was mode %o, tightening to 600", mode)
                self.path.chmod(0o600)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SessionError(f"no session at {self.path}, run `wmbot login`") from None
        except (OSError, ValueError) as err:
            raise SessionError(f"cannot read {self.path}: {type(err).__name__}") from None
        return session_from_mapping(raw)

    def save(self, session: Session) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "access_token": session.access_token,
                        "refresh_token": session.refresh_token,
                        "expires_at": session.expires_at,
                    },
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(f"another wmbot process holds {self.path}") from None
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def is_held_elsewhere(self) -> bool:
        if not self.path.exists():
            return False
        try:
            self.acquire()
        except AlreadyRunning:
            return True
        self.release()
        return False

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def build_cookie(session: Session, ref: str) -> str:
    payload = json.dumps(
        {
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expires_at": session.expires_at,
            "token_type": "bearer",
            "expires_in": 3600,
        }
    ).encode()
    value = "base64-" + base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return "; ".join(
        f"sb-{ref}-auth-token.{index}={value[start:start + COOKIE_CHUNK]}"
        for index, start in enumerate(range(0, len(value), COOKIE_CHUNK))
    )


def session_from_set_cookies(set_cookies: list[str], ref: str) -> Session | None:
    pattern = re.compile(rf"^sb-{re.escape(ref)}-auth-token(?:\.(\d+))?=([^;]*)")
    parts: dict[int, str] = {}
    for header in set_cookies:
        match = pattern.match(header.strip())
        if match and match.group(2):
            parts[int(match.group(1) or 0)] = match.group(2)
    if not parts:
        return None
    value = "".join(parts[index] for index in sorted(parts))
    if value.startswith("base64-"):
        encoded = value.removeprefix("base64-")
        try:
            value = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
    try:
        return session_from_mapping(json.loads(value))
    except (ValueError, SessionError):
        return None


ANON_JWT = re.compile(r"eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]+")
CHUNK_URL = re.compile(r'/_next/static/[^"\\\s?]+\.js')


def _jwt_payload(token: str) -> dict[str, object]:
    encoded = token.split(".")[1]
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (binascii.Error, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def discover_anon_key(config: Config) -> str:
    headers = {"User-Agent": config.user_agent}
    page = request("GET", f"{config.site_url}/login", headers=headers)
    for path in sorted(set(CHUNK_URL.findall(page.body.decode("utf-8", "replace")))):
        chunk = request("GET", config.site_url + path, headers=headers).body.decode("utf-8", "replace")
        for token in ANON_JWT.findall(chunk):
            claims = _jwt_payload(token)
            if claims.get("role") == "anon" and claims.get("ref") == config.supabase_ref:
                return token
    raise NetworkError("could not find the public anon key on the site, set WM_ANON_KEY")


def auth_error_code(response: Response) -> str:
    data = response.json()
    if isinstance(data, dict):
        for key in ("error_code", "code", "error"):
            value = data.get(key)
            if isinstance(value, (str, int)):
                return str(value)[:60]
    return "unknown"


class Authenticator:
    def __init__(
        self,
        config: Config,
        store: SessionStore,
        session: Session,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.store = store
        self.session = session
        self._sleep = sleep
        self._anon_key = config.anon_key
        self._unsaved = False

    def anon_key(self) -> str:
        if not self._anon_key:
            self._anon_key = discover_anon_key(self.config)
        return self._anon_key

    def _post_refresh(self) -> Response:
        return request(
            "POST",
            f"{self.config.supabase_url}/auth/v1/token?grant_type=refresh_token",
            data=json.dumps({"refresh_token": self.session.refresh_token}).encode(),
            headers={
                "apikey": self.anon_key(),
                "Content-Type": "application/json",
                "User-Agent": self.config.user_agent,
            },
            timeout=20,
        )

    def refresh(self) -> Session:
        try:
            try:
                response = self._post_refresh()
            except NetworkError:
                self._sleep(REUSE_RETRY_DELAY)
                response = self._post_refresh()
            if response.status >= 500:
                self._sleep(REUSE_RETRY_DELAY)
                response = self._post_refresh()
        except NetworkError as err:
            raise TransientAuthError(f"refresh network error: {err}") from None
        if response.status in TERMINAL_AUTH_STATUSES:
            raise FatalAuthError(
                f"refresh rejected ({response.status} {auth_error_code(response)}), a new refresh token is needed"
            )
        if response.status != 200:
            raise TransientAuthError(f"refresh returned {response.status}")
        try:
            fresh = session_from_mapping(response.json())
        except SessionError:
            raise TransientAuthError("refresh response had no token") from None
        if not fresh.access_token:
            raise TransientAuthError("refresh response had no access token")
        self.adopt(fresh)
        log.info("session refreshed, expires in %ds", fresh.expires_in())
        return fresh

    def adopt(self, session: Session) -> None:
        self.session = session
        self._unsaved = True
        self.flush()

    def flush(self) -> None:
        if not self._unsaved:
            return
        try:
            self.store.save(self.session)
            self._unsaved = False
        except OSError as err:
            log.error("cannot persist rotated session, will retry: %s", type(err).__name__)

    def ensure_fresh(self, margin: float = REFRESH_MARGIN) -> Session:
        self.flush()
        if not self.session.access_token or self.session.expires_in() < margin:
            return self.refresh()
        return self.session

    def absorb_cookies(self, response: Response) -> None:
        rotated = session_from_set_cookies(response.set_cookies, self.config.supabase_ref)
        if rotated and rotated.access_token and rotated.refresh_token != self.session.refresh_token:
            log.info("server rotated the session through Set-Cookie, persisting it")
            self.adopt(rotated)
