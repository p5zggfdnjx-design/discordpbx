#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${DISCORDPBX_REPO:-p5zggfdnjx-design/discordpbx}"
CONTAINER_NAME="${PBX_CONTAINER_NAME:-discord-pbx}"
INSTALL_DIR="${PBX_INSTALL_DIR:-/opt/discord-pbx}"
OLD_DIR="${PBX_OLD_DIR:-}"
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK=""
OLD_WAS_RUNNING=0

log(){ printf '\n==> %s\n' "$*"; }
die(){ echo "ERROR: $*" >&2; exit 1; }
cleanup(){ [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"; }
trap cleanup EXIT

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "Run as root/sudo."
command -v docker >/dev/null || die "Docker is required."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
command -v curl >/dev/null || die "curl is required."
command -v python3 >/dev/null || die "python3 is required."
command -v ss >/dev/null || die "iproute2/ss is required."

if ! command -v rsync >/dev/null || ! command -v unzip >/dev/null; then
  apt-get update
  apt-get install -y rsync unzip
fi

if [[ -z "$OLD_DIR" ]] && docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  OLD_DIR="$(docker inspect "$CONTAINER_NAME" --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' 2>/dev/null || true)"
  if [[ "$(docker inspect "$CONTAINER_NAME" --format '{{.State.Running}}' 2>/dev/null || true)" == "true" ]]; then
    OLD_WAS_RUNNING=1
  fi
fi
[[ -z "$OLD_DIR" || -d "$OLD_DIR" ]] || die "Current project directory does not exist: $OLD_DIR"

log "Current deployment"
echo "Container: $CONTAINER_NAME"
echo "Current directory: ${OLD_DIR:-none detected}"
echo "Managed directory: $INSTALL_DIR"

WORK="$(mktemp -d /tmp/discordpbx-managed-install.XXXXXX)"
RELEASE_JSON="$WORK/release.json"
PACKAGE="$WORK/release.zip"
EXTRACT="$WORK/extract"
mkdir -p "$EXTRACT"

log "Downloading latest verified GitHub release"
curl -fsSL --retry 3 --retry-delay 2 -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/$REPO/releases/latest" -o "$RELEASE_JSON"
readarray -t RELEASE_INFO < <(python3 - "$RELEASE_JSON" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf-8'))
tag=str(p.get('tag_name') or '')
assets=p.get('assets') or []
zips=[a for a in assets if str(a.get('name','')).lower().endswith('.zip') and not str(a.get('name','')).lower().endswith('.zip.sha256')]
preferred=[a for a in zips if 'discord-freepbx-bridge' in str(a.get('name','')).lower()]
a=(preferred or zips or [None])[0]
if not tag or not a or not a.get('browser_download_url'):
    raise SystemExit('Latest release does not contain a DiscordPBX ZIP asset')
print(tag)
print(a['browser_download_url'])
print(a.get('digest') or '')
PY
)
TAG="${RELEASE_INFO[0]}"
URL="${RELEASE_INFO[1]}"
EXPECTED_DIGEST="${RELEASE_INFO[2]:-}"
curl -fL --retry 3 --retry-delay 2 "$URL" -o "$PACKAGE"
ACTUAL_SHA="$(sha256sum "$PACKAGE" | awk '{print $1}')"
if [[ "$EXPECTED_DIGEST" == sha256:* ]]; then
  [[ "${EXPECTED_DIGEST#sha256:}" == "$ACTUAL_SHA" ]] || die "Downloaded release SHA-256 does not match GitHub metadata."
fi

echo "Release: $TAG"
echo "SHA-256: $ACTUAL_SHA"

python3 - "$PACKAGE" "$EXTRACT" <<'PY'
import os,sys,zipfile
src,dst=sys.argv[1:]
with zipfile.ZipFile(src) as z:
    for i in z.infolist():
        name=i.filename.replace('\\','/').lstrip('/')
        parts=[p for p in name.split('/') if p not in ('','.')]
        if '..' in parts: raise SystemExit('unsafe ZIP path')
        target=os.path.realpath(os.path.join(dst,*parts)) if parts else os.path.realpath(dst)
        if target != os.path.realpath(dst) and not target.startswith(os.path.realpath(dst)+os.sep):
            raise SystemExit('unsafe ZIP target')
        if i.is_dir():
            os.makedirs(target,exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target),exist_ok=True)
        with z.open(i) as r, open(target,'wb') as w:
            while True:
                b=r.read(1024*1024)
                if not b: break
                w.write(b)
PY

SOURCE="$EXTRACT"
mapfile -t TOPDIRS < <(find "$EXTRACT" -mindepth 1 -maxdepth 1 -type d -printf '%p\n')
mapfile -t TOPFILES < <(find "$EXTRACT" -mindepth 1 -maxdepth 1 -type f -printf '%p\n')
if [[ ${#TOPDIRS[@]} -eq 1 && ${#TOPFILES[@]} -eq 0 ]]; then
  SOURCE="${TOPDIRS[0]}"
fi
for req in bot.py config.py docker-compose.yml install-managed-updater.sh updater/managed-update-agent.sh; do
  [[ -f "$SOURCE/$req" ]] || die "Release package is missing $req"
done

contact_count(){
  local path="$1"
  python3 - "$path" <<'PY'
import json,sys
p=sys.argv[1]
try:
    d=json.load(open(p,encoding='utf-8'))
    print(len(d) if isinstance(d,list) else 0)
except Exception:
    print(-1)
PY
}
OLD_CONTACTS=0
if [[ -n "$OLD_DIR" && -f "$OLD_DIR/data/contacts.json" ]]; then
  OLD_CONTACTS="$(contact_count "$OLD_DIR/data/contacts.json")"
  [[ "$OLD_CONTACTS" -ge 0 ]] || die "Existing contacts.json is invalid; refusing migration."
fi

log "Building stable managed installation"
mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/data"
# Keep runtime state and local host overrides out of release replacement.
rsync -a --delete \
  --exclude 'data/' \
  --exclude '.env' \
  --exclude '.git/' \
  --exclude 'docker-compose.override.yml' \
  "$SOURCE/" "$INSTALL_DIR/"

if [[ -n "$OLD_DIR" && "$OLD_DIR" != "$INSTALL_DIR" ]]; then
  if [[ -f "$OLD_DIR/.env" ]]; then
    cp -p "$OLD_DIR/.env" "$INSTALL_DIR/.env"
  fi
  if [[ -d "$OLD_DIR/data" ]]; then
    rsync -a "$OLD_DIR/data/" "$INSTALL_DIR/data/"
  fi
elif [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi

mkdir -p "$INSTALL_DIR/data/recovery"
if [[ -f "$INSTALL_DIR/data/contacts.json" ]]; then
  cp -a "$INSTALL_DIR/data/contacts.json" "$INSTALL_DIR/data/recovery/contacts-before-managed-$STAMP.json"
fi
NEW_CONTACTS=0
if [[ -f "$INSTALL_DIR/data/contacts.json" ]]; then
  NEW_CONTACTS="$(contact_count "$INSTALL_DIR/data/contacts.json")"
  [[ "$NEW_CONTACTS" -ge "$OLD_CONTACTS" ]] || die "Managed migration would reduce contacts from $OLD_CONTACTS to $NEW_CONTACTS; refusing cutover."
fi

echo "Contacts before: $OLD_CONTACTS"
echo "Contacts after:  $NEW_CONTACTS"

# Resolve real upstream DNS servers. Docker cannot use the host's 127.0.0.53
# systemd-resolved stub from inside a container.
mapfile -t DNS_SERVERS < <(
  {
    awk '/^nameserver[[:space:]]+/ {print $2}' /etc/resolv.conf 2>/dev/null || true
    if command -v resolvectl >/dev/null 2>&1; then
      resolvectl dns 2>/dev/null | awk '{for(i=3;i<=NF;i++) print $i}' || true
    fi
  } | awk '
    $0 ~ /^[0-9a-fA-F:.]+$/ &&
    $0 != "127.0.0.1" && $0 != "127.0.0.53" && $0 != "::1" { if(!seen[$0]++) print $0 }
  ' | head -n 3
)
if [[ ${#DNS_SERVERS[@]} -eq 0 ]]; then
  DNS_SERVERS=("1.1.1.1" "8.8.8.8")
fi

{
  echo "# Host-local settings. Preserved across managed DiscordPBX updates."
  echo "services:"
  echo "  discord-pbx:"
  echo "    init: true"
  echo "    dns:"
  for dns in "${DNS_SERVERS[@]}"; do printf '      - "%s"\n' "$dns"; done
} > "$INSTALL_DIR/docker-compose.override.yml"
chmod 0644 "$INSTALL_DIR/docker-compose.override.yml"

env_value(){
  local key="$1" default="$2"
  python3 - "$INSTALL_DIR/.env" "$key" "$default" <<'PY'
import sys
path,key,default=sys.argv[1:]
value=default
try:
    for raw in open(path,encoding='utf-8'):
        s=raw.strip()
        if not s or s.startswith('#') or '=' not in s: continue
        k,v=s.split('=',1)
        if k.strip()==key:
            value=v.split('#',1)[0].strip().strip('"').strip("'") or default
except FileNotFoundError:
    pass
print(value)
PY
}
WEB_PORT="$(env_value WEB_PORT 8088)"
AUDIO_PORT="$(env_value AUDIOSOCKET_PORT 9092)"

cd "$INSTALL_DIR"
docker compose config >/dev/null
log "Building replacement image before touching the old service"
docker compose build

listener_pids(){
  local port="$1"
  ss -H -ltnp "sport = :$port" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true
}
recognized_legacy_pid(){
  local pid="$1" cmd cwd
  cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ -n "$OLD_DIR" && ( "$cwd" == "$OLD_DIR"* || "$cmd" == *"$OLD_DIR"* ) ]] && return 0
  [[ "$cmd" == *"discord-freepbx-bridge"* || "$cmd" == *"discordpbx"* || "$cmd" == *"discord-pbx"* ]] && return 0
  return 1
}

rollback_old(){
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  if [[ "$OLD_WAS_RUNNING" -eq 1 && -n "$OLD_DIR" && "$OLD_DIR" != "$INSTALL_DIR" && -f "$OLD_DIR/docker-compose.yml" ]]; then
    echo "Attempting rollback to $OLD_DIR" >&2
    (cd "$OLD_DIR" && docker compose up -d) || true
  fi
}

log "Cutting over to the managed service"
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
sleep 1
for port in "$WEB_PORT" "$AUDIO_PORT"; do
  mapfile -t PIDS < <(listener_pids "$port")
  for pid in "${PIDS[@]}"; do
    [[ -d "/proc/$pid" ]] || continue
    if recognized_legacy_pid "$pid"; then
      echo "Stopping stale DiscordPBX host listener PID $pid on TCP $port"
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    else
      echo "TCP $port is occupied by an unrelated host process:" >&2
      ps -fp "$pid" >&2 || true
      rollback_old
      die "Free TCP $port, then rerun this installer."
    fi
  done
done

if ! docker compose up -d --remove-orphans; then
  rollback_old
  die "Managed container failed to start."
fi

log "Waiting for health and DNS"
healthy=0
for _ in $(seq 1 60); do
  state="$(docker inspect "$CONTAINER_NAME" --format '{{.State.Status}}' 2>/dev/null || true)"
  health="$(docker inspect "$CONTAINER_NAME" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
  if [[ "$state" == "running" && ( "$health" == "healthy" || -z "$health" ) ]]; then
    if docker exec "$CONTAINER_NAME" python - <<'PY' >/dev/null 2>&1
import socket
socket.getaddrinfo('discord.com',443)
PY
    then
      healthy=1
      break
    fi
  fi
  if [[ "$state" == "exited" || "$state" == "dead" || "$health" == "unhealthy" ]]; then
    break
  fi
  sleep 2
done

if [[ "$healthy" -ne 1 ]]; then
  docker compose logs --tail=150 >&2 || true
  rollback_old
  die "Managed install did not become healthy with working Discord DNS; old deployment was restored when possible."
fi

chmod +x "$INSTALL_DIR/install-managed-updater.sh" "$INSTALL_DIR/bootstrap-update.sh" "$INSTALL_DIR/upgrade-from-current.sh" "$INSTALL_DIR/updater/managed-update-agent.sh" 2>/dev/null || true
"$INSTALL_DIR/install-managed-updater.sh"

log "Managed DiscordPBX is ready"
docker compose ps
VERSION="$(docker exec "$CONTAINER_NAME" python - <<'PY' 2>/dev/null || true
from config import Config
print(Config.from_env().version)
PY
)"
CONTACTS="$(docker exec "$CONTAINER_NAME" python - <<'PY' 2>/dev/null || true
from config import Config
from contacts import ContactsStore
c=Config.from_env(); print(len(ContactsStore(c.contacts_file).list()))
PY
)"
echo "Version: ${VERSION:-unknown}"
echo "Contacts: ${CONTACTS:-unknown}"
echo "Managed project: $INSTALL_DIR"
echo "Persistent data: $INSTALL_DIR/data"
echo "Host override: $INSTALL_DIR/docker-compose.override.yml"
echo "Future upgrades: Settings -> Updates -> Update latest from GitHub"
