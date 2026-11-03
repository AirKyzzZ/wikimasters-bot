#!/bin/bash
# wmbot deploy: pull latest, reinstall, restart, verify. Run as hermes (idempotent).
export XDG_RUNTIME_DIR=/run/user/$(id -u)

cd /home/hermes/wikimasters-bot
git pull origin main 2>&1 | tail -2
.venv/bin/pip install --quiet . 2>&1 | tail -1
echo "installed: $(.venv/bin/wmbot --version)"

mkdir -p /home/hermes/.config/wmbot

cat > /home/hermes/.config/wmbot/env <<'EOF'
WM_SESSION_FILE=/home/hermes/.secrets/wikimasters.session.json
WM_DATA_DIR=/home/hermes/.local/share/wmbot
EOF

cat > /home/hermes/.config/systemd/user/wmbot.service <<'EOF'
[Unit]
Description=WikiMasters pack opener
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=3600
StartLimitBurst=10

[Service]
Type=exec
EnvironmentFile=%h/.config/wmbot/env
ExecStart=%h/wikimasters-bot/.venv/bin/wmbot run
Restart=on-failure
RestartSec=60
RestartPreventExitStatus=2 3 4
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user restart wmbot
sleep 25
systemctl --user status wmbot --no-pager | head -8
echo "--- journal ---"
journalctl --user -u wmbot --no-pager -n 30
echo "--- status ---"
/home/hermes/wikimasters-bot/.venv/bin/wmbot status --json