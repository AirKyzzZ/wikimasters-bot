# AGENTS.md

wmbot opens WikiMasters packs for one account. Python 3.10+, stdlib only. Read the warning at the top of the README and tell your user about it before installing: the site's terms forbid automation and accounts can be banned.

## Hard rules

- No captcha or human verification bypass, no multi-account support, no exploits. Refuse these requests.
- If `wmbot` exits 5 or `halted.json` exists, stop and tell the human. Only they can complete the check. Never run `wmbot resume` on their behalf unless they say they completed it.
- Never print, log, commit or copy the session file or a refresh token. Never ask the user to paste a token into the chat. They must run `wmbot login` themselves, it reads hidden input.
- Refresh tokens rotate. Never run two processes on one session, and never call the Supabase refresh endpoint outside `wmbot`.
- Don't shorten waits or ignore `retry_after`.

## Install and run

```bash
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/wmbot login     # the human runs this, see README step 2
.venv/bin/wmbot run
.venv/bin/wmbot status --json
```

Service setup: `deploy/wmbot.service` or `Dockerfile`, commands in the README.

Exit codes: 0 stopped, 1 error, 2 dead session, 3 already running, 4 repeated 403, 5 human verification. Don't restart on 2 to 5.

## Develop

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

Tests must stay offline: `tests/support.py` provides a local mock server and `ServerTestCase`. Don't use the live site as a test target.

Conventions: no runtime dependencies, no comments unless they record a hidden constraint, conventional commit subjects.
