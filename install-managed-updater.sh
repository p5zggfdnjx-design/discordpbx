#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this once with sudo: sudo ./install-managed-updater.sh" >&2
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_SRC="$PROJECT_DIR/updater/managed-update-agent.sh"
AGENT_DST="/usr/local/sbin/discord-pbx-managed-update"
SERVICE="/etc/systemd/system/discord-pbx-update.service"
PATHUNIT="/etc/systemd/system/discord-pbx-update.path"

[[ -f "$PROJECT_DIR/docker-compose.yml" ]] || { echo "Run this from the DiscordPBX project directory." >&2; exit 1; }
[[ -x "$AGENT_SRC" ]] || { echo "Missing updater agent: $AGENT_SRC" >&2; exit 1; }
command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose v2 is required." >&2; exit 1; }

if ! command -v rsync >/dev/null || ! command -v flock >/dev/null; then
  apt-get update
  apt-get install -y rsync util-linux
fi

install -m 0755 "$AGENT_SRC" "$AGENT_DST"
mkdir -p "$PROJECT_DIR/data/updates"
PROJECT_UID="$(stat -c %u "$PROJECT_DIR")"
PROJECT_GID="$(stat -c %g "$PROJECT_DIR")"
chown -R "$PROJECT_UID:$PROJECT_GID" "$PROJECT_DIR/data/updates"
chmod 0770 "$PROJECT_DIR/data/updates"

cat > "$SERVICE" <<UNIT
[Unit]
Description=DiscordPBX managed update
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
Environment=PROJECT_DIR=$PROJECT_DIR
Environment=CONTAINER_NAME=discord-pbx
ExecStart=$AGENT_DST
Nice=5
IOSchedulingClass=best-effort
UNIT

cat > "$PATHUNIT" <<UNIT
[Unit]
Description=Watch for DiscordPBX update requests

[Path]
PathExists=$PROJECT_DIR/data/updates/apply.json
Unit=discord-pbx-update.service

[Install]
WantedBy=multi-user.target
UNIT

python3 - "$PROJECT_DIR/data/updates/status.json" "$PROJECT_DIR" <<'PY'
import json,os,sys,time
path,project=sys.argv[1:]
os.makedirs(os.path.dirname(path),exist_ok=True)
json.dump({"managed":True,"project_dir":project,"state":"ready","version":"","detail":"Managed updater installed","updated_at":time.time()},open(path,'w',encoding='utf-8'),indent=2)
PY
chown "$PROJECT_UID:$PROJECT_GID" "$PROJECT_DIR/data/updates/status.json"
chmod 0660 "$PROJECT_DIR/data/updates/status.json"

systemctl daemon-reload
systemctl enable --now discord-pbx-update.path
systemctl start discord-pbx-update.path

echo "Managed updater installed for: $PROJECT_DIR"
echo "Settings -> Updates can now stage and install future ZIP releases."
