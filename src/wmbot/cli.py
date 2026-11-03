from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from wmbot import __version__
from wmbot.config import Config, ConfigError, load_config
from wmbot.net import NetworkError, RedactingFilter
from wmbot.pulls import read_pulls
from wmbot.report import post_discord, render_markdown, summarize, write_daily_report
from wmbot.runner import Bot, ForbiddenError, HumanVerificationRequired, read_halt
from wmbot.session import (
    AlreadyRunning,
    Authenticator,
    FatalAuthError,
    ProcessLock,
    Session,
    SessionError,
    SessionStore,
    TransientAuthError,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2
EXIT_ALREADY_RUNNING = 3
EXIT_FORBIDDEN = 4
EXIT_VERIFICATION = 5
LOCK_ATTEMPTS = 5
LOCK_RETRY_SECONDS = 2.0

log = logging.getLogger("wmbot")


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S"))
    handler.addFilter(RedactingFilter())
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def acquire_with_retry(lock: ProcessLock) -> None:
    for attempt in range(LOCK_ATTEMPTS):
        try:
            lock.acquire()
            return
        except AlreadyRunning:
            if attempt == LOCK_ATTEMPTS - 1:
                raise
            time.sleep(LOCK_RETRY_SECONDS)


def cmd_run(config: Config, _: argparse.Namespace) -> int:
    halt = read_halt(config.halt_file)
    if halt and config.on_verification == "exit":
        log.error("halted: %s", halt.get("reason"))
        return EXIT_VERIFICATION
    lock = ProcessLock(config.lock_file)
    acquire_with_retry(lock)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    store = SessionStore(config.session_file)
    auth = Authenticator(config, store, store.load())
    bot = Bot(config, auth, stop=stop)
    log.info("wmbot %s started", __version__)
    try:
        bot.run()
    except FatalAuthError as err:
        bot.alert(str(err))
        return EXIT_AUTH
    except ForbiddenError as err:
        bot.alert(str(err))
        return EXIT_FORBIDDEN
    except HumanVerificationRequired:
        return EXIT_VERIFICATION
    finally:
        lock.release()
    log.info("stopped")
    return EXIT_OK


def cmd_resume(config: Config, args: argparse.Namespace) -> int:
    if read_halt(config.halt_file) is None:
        print("not paused")
        return EXIT_OK
    if not args.yes:
        if not sys.stdin.isatty():
            print("run this in a terminal, or pass --yes once you have passed the check", file=sys.stderr)
            return EXIT_ERROR
        if input("Did you pass the check yourself in your browser? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("still paused")
            return EXIT_OK
    config.halt_file.unlink(missing_ok=True)
    print("resuming: a running bot continues within seconds, otherwise start it again")
    return EXIT_OK


def cmd_login(config: Config, args: argparse.Namespace) -> int:
    store = SessionStore(config.session_file)
    if config.session_file.exists() and not args.force:
        print(f"{config.session_file} already exists, use --force to replace it", file=sys.stderr)
        return EXIT_ERROR
    prompt = "Refresh token (input hidden): "
    token = (getpass.getpass(prompt) if sys.stdin.isatty() else sys.stdin.readline()).strip()
    if not token or any(char.isspace() for char in token):
        print("that does not look like a refresh token", file=sys.stderr)
        return EXIT_ERROR
    with ProcessLock(config.lock_file):
        Authenticator(config, store, Session("", token, 0)).refresh()
    print(f"session saved to {config.session_file}")
    return EXIT_OK


def _human_delta(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    minutes = int(abs(seconds)) // 60
    return f"{sign}{minutes // 60}h{minutes % 60:02d}m"


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    running = ProcessLock(config.lock_file).is_held_elsewhere()
    info: dict[str, object] = {"version": __version__, "running": running, "session_file": str(config.session_file)}
    halt = read_halt(config.halt_file)
    info["halted"] = halt
    try:
        session = SessionStore(config.session_file).load()
        info["access_token_expires_in_s"] = int(session.expires_in())
    except SessionError as err:
        info["session_error"] = str(err)
    try:
        info["state"] = json.loads(config.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info["state"] = None
    today = dt.datetime.now().astimezone().date()
    pulls = list(read_pulls(config.pulls_file))
    info["packs_total"] = len(pulls)
    info["packs_today"] = sum(1 for pull in pulls if pull.at.astimezone().date() == today)
    info["last_pull_at"] = pulls[-1].at.isoformat() if pulls else None
    if args.json:
        print(json.dumps(info, indent=2))
        return EXIT_OK
    print(f"wmbot {__version__}: {'running' if running else 'not running'}")
    if halt:
        print(f"PAUSED:   {halt.get('reason')} ({halt.get('at', 'unknown time')})")
    if "session_error" in info:
        print(f"session:  {info['session_error']}")
    else:
        print(f"session:  access token expires in {_human_delta(session.expires_in())}")
    print(f"packs:    {info['packs_today']} today, {info['packs_total']} total, last {info['last_pull_at'] or 'never'}")
    state = info["state"]
    if isinstance(state, dict):
        print(f"last run: {state.get('outcome')}, {state.get('packs_remaining')} packs left")
        print(f"next try: {state.get('next_attempt_at')}")
        if state.get("consecutive_errors"):
            print(f"errors:   {state['consecutive_errors']} in a row")
    return EXIT_OK


def cmd_report(config: Config, args: argparse.Namespace) -> int:
    today = dt.datetime.now().astimezone().date()
    day = dt.date.fromisoformat(args.date) if args.date else today
    if args.stdout:
        summary = summarize(config.pulls_file, day)
        print(render_markdown(summary), end="")
        if args.discord:
            post_discord(config, summary)
        return EXIT_OK
    no_discord = replace(config, discord_webhook="")
    target = write_daily_report(no_discord, day)
    print(f"wrote {target}")
    if args.discord:
        if not config.discord_webhook:
            print("WM_DISCORD_WEBHOOK is not set", file=sys.stderr)
            return EXIT_ERROR
        post_discord(config, summarize(config.pulls_file, day))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wmbot", description="Opens your WikiMasters packs and reports what you got.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", type=Path, help="env file to read (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the bot until stopped").set_defaults(func=cmd_run)
    resume = sub.add_parser("resume", help="continue after you passed the human verification yourself")
    resume.add_argument("--yes", action="store_true", help="skip the question, for non-interactive shells")
    resume.set_defaults(func=cmd_resume)
    login = sub.add_parser("login", help="store your refresh token (asked with hidden input)")
    login.add_argument("--force", action="store_true", help="replace an existing session")
    login.set_defaults(func=cmd_login)
    status = sub.add_parser("status", help="show bot state, offline")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    report = sub.add_parser("report", help="write the daily report")
    report.add_argument("--date", help="YYYY-MM-DD, default today")
    report.add_argument("--stdout", action="store_true", help="print instead of writing a file")
    report.add_argument("--discord", action="store_true", help="also post to WM_DISCORD_WEBHOOK")
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        config = load_config(env_file=args.env_file)
        return args.func(config, args)
    except ConfigError as err:
        log.error("config: %s", err)
        return EXIT_ERROR
    except AlreadyRunning as err:
        log.error("%s", err)
        return EXIT_ALREADY_RUNNING
    except FatalAuthError as err:
        log.error("%s", err)
        return EXIT_AUTH
    except (SessionError, TransientAuthError, NetworkError, ValueError) as err:
        log.error("%s", err)
        return EXIT_ERROR
