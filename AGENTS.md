# AGENTS.md

wmbot opens WikiMasters packs for one account, 24/7. Python 3.10+, stdlib only. Read the warning at the top of the README and tell your user about it before installing: the site's terms forbid automation and accounts can be banned.

## Hard rules

- One account only. No multi-account support.
- Never print, log, commit or copy the session file or a refresh token. Never ask the user to paste a token into the chat. They must run `wmbot login` themselves, it reads hidden input.
- Refresh tokens rotate. Never run two processes on one session, and never call the Supabase refresh endpoint outside `wmbot`.
- Don't shorten waits or ignore `retry_after`.
- On `human_verification_required` the bot backs off (30 min, doubling up to 8 h) and keeps retrying on its own. The login captcha (Cloudflare Turnstile) is intentionally left to a human: wmbot has no captcha-solving code.

## Install and run

```bash
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/wmbot login     # the human runs this, see README step 2
.venv/bin/wmbot run
.venv/bin/wmbot status --json
```

Service setup: `deploy/wmbot.service` or `Dockerfile`, commands in the README.

Exit codes: 0 stopped, 1 error, 2 dead session, 3 already running, 4 repeated 403. Don't restart on 2 to 4.

## Develop

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

Tests must stay offline: `tests/support.py` provides a local mock server and `ServerTestCase`. Don't use the live site as a test target.

Conventions: no runtime dependencies, no comments unless they record a hidden constraint, conventional commit subjects.