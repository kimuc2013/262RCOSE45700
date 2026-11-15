#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo' >&2; exit 2; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null
command -v git >/dev/null
command -v curl >/dev/null
docker compose version >/dev/null
install -d -m 755 /opt/ku-academic-mcp
install -m 644 "$source_dir/autodeploy.py" /opt/ku-academic-mcp/autodeploy.py
cat > /etc/systemd/system/ku-mcp-autodeploy.service <<'EOF'
[Unit]
Description=Deploy CI-approved academic MCP main
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/ku-academic-mcp
ExecStart=/usr/bin/python3 -u /opt/ku-academic-mcp/autodeploy.py
TimeoutStartSec=25min
UMask=0077
EOF
cat > /etc/systemd/system/ku-mcp-autodeploy.timer <<'EOF'
[Unit]
Description=Check academic MCP main after each two minute interval

[Timer]
OnBootSec=60s
OnUnitInactiveSec=120s
AccuracySec=10s
Unit=ku-mcp-autodeploy.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now ku-mcp-autodeploy.timer
systemctl start ku-mcp-autodeploy.service
systemctl list-timers ku-mcp-autodeploy.timer --no-pager
