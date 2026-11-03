from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SUPABASE_REF = "cyrxjeppjqsxxjayfrur"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    session_file: Path
    data_dir: Path
    site_url: str = "https://www.wiki-masters.com"
    supabase_url: str = f"https://{SUPABASE_REF}.supabase.co"
    supabase_ref: str = SUPABASE_REF
    anon_key: str = ""
    discord_webhook: str = ""
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) wmbot (personal, one account)"
    auto_report: bool = True

    @property
    def pulls_file(self) -> Path:
        return self.data_dir / "pulls.jsonl"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def halt_file(self) -> Path:
        return self.data_dir / "halted.json"

    @property
    def lock_file(self) -> Path:
        return self.session_file.with_name(self.session_file.name + ".lock")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _require_https(name: str, url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        return url.rstrip("/")
    raise ConfigError(f"{name} must be https, tokens are never sent in clear text")


def _require_discord(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    allowed = parts.hostname in {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
    if parts.scheme == "https" and allowed and parts.path.startswith("/api/webhooks/"):
        return url
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return url
    raise ConfigError("WM_DISCORD_WEBHOOK must be a https://discord.com/api/webhooks/... URL")


def load_config(env: dict[str, str] | None = None, env_file: Path | None = None) -> Config:
    environ = dict(os.environ if env is None else env)
    path = env_file or Path(environ.get("WM_ENV_FILE", ".env"))
    merged = {**(parse_env_file(path) if path.is_file() else {}), **environ}

    def get(name: str, default: str) -> str:
        return merged.get(name) or default

    return Config(
        session_file=Path(get("WM_SESSION_FILE", "~/.secrets/wikimasters.session.json")).expanduser(),
        data_dir=Path(get("WM_DATA_DIR", "~/.local/share/wmbot")).expanduser(),
        site_url=_require_https("WM_SITE_URL", get("WM_SITE_URL", Config.site_url)),
        supabase_url=_require_https("WM_SUPABASE_URL", get("WM_SUPABASE_URL", Config.supabase_url)),
        anon_key=get("WM_ANON_KEY", ""),
        discord_webhook=_require_discord(get("WM_DISCORD_WEBHOOK", "")),
        user_agent=get("WM_USER_AGENT", Config.user_agent),
        auto_report=get("WM_AUTO_REPORT", "1").lower() not in {"0", "false", "no", "off"},
    )
