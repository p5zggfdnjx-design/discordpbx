#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/discord-pbx}"
CONTAINER_NAME="${CONTAINER_NAME:-discord-pbx}"
UPDATES_DIR="$PROJECT_DIR/data/updates"
PENDING="$UPDATES_DIR/pending.zip"
META="$UPDATES_DIR/pending_meta.json"
APPLY="$UPDATES_DIR/apply.json"
STATUS="$UPDATES_DIR/status.json"
LOCK="/run/lock/discord-pbx-update.lock"

mkdir -p "$UPDATES_DIR" "$(dirname "$LOCK")"
# Keep the queue writable by the project owner as well as the root-owned host
# updater. This avoids rsync/status-file ownership traps after migrations.
PROJECT_UID="$(stat -c %u "$PROJECT_DIR")"
PROJECT_GID="$(stat -c %g "$PROJECT_DIR")"
chown "$PROJECT_UID:$PROJECT_GID" "$UPDATES_DIR" 2>/dev/null || true
chmod 0770 "$UPDATES_DIR" 2>/dev/null || true
exec 9>"$LOCK"
flock -n 9 || exit 0

write_status() {
  local state="$1" version="${2:-}" detail="${3:-}"
  python3 - "$STATUS" "$PROJECT_DIR" "$state" "$version" "$detail" <<'PY'
import json, os, sys, tempfile, time
path, project, state, version, detail = sys.argv[1:]
data = {
    "managed": True,
    "project_dir": project,
    "state": state,
    "version": version,
    "detail": detail,
    "updated_at": time.time(),
}
os.makedirs(os.path.dirname(path), exist_ok=True)
fd,tmp=tempfile.mkstemp(prefix="status-",suffix=".json",dir=os.path.dirname(path))
with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(data,f,indent=2)
os.replace(tmp,path)
PY
}

fail() {
  rm -f "$APPLY" 2>/dev/null || true
  write_status "failed" "${TARGET_VERSION:-}" "$*"
  echo "DiscordPBX update failed: $*" >&2
  exit 1
}

[[ -f "$APPLY" ]] || exit 0
[[ -f "$PENDING" ]] || fail "apply marker exists but pending.zip is missing"
[[ -f "$PROJECT_DIR/docker-compose.yml" ]] || fail "docker-compose.yml missing from $PROJECT_DIR"

TARGET_VERSION="$(python3 - "$META" <<'PY'
import json,sys
try:
    print(json.load(open(sys.argv[1],encoding='utf-8')).get('version','unknown'))
except Exception:
    print('unknown')
PY
)"
EXPECTED_SHA="$(python3 - "$APPLY" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1],encoding='utf-8')).get('sha256',''))
except Exception: print('')
PY
)"
ACTUAL_SHA="$(sha256sum "$PENDING" | awk '{print $1}')"
[[ -z "$EXPECTED_SHA" || "$EXPECTED_SHA" == "$ACTUAL_SHA" ]] || fail "package SHA-256 does not match staged metadata"

write_status "backing-up" "$TARGET_VERSION" "Creating rollback snapshot"
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d /tmp/discord-pbx-update.XXXXXX)"
ROLLBACK="$WORK/rollback"
DATA_ROLLBACK="$WORK/data-rollback"
ENV_ROLLBACK="$WORK/env.rollback"
EXTRACT="$WORK/extract"
mkdir -p "$ROLLBACK" "$DATA_ROLLBACK" "$EXTRACT"
cleanup(){ rm -rf "$WORK"; }
trap cleanup EXIT

restore_runtime_state() {
  # The updater queue/status lives under data/updates and must not be restored or
  # a failed update could immediately retrigger itself. Everything else is
  # restored to the consistent pre-update snapshot.
  if [[ -d "$DATA_ROLLBACK" ]]; then
    rsync -a --delete --exclude 'updates/' "$DATA_ROLLBACK/" "$PROJECT_DIR/data/" || true
  fi
  if [[ -f "$ENV_ROLLBACK" ]]; then
    cp -a "$ENV_ROLLBACK" "$PROJECT_DIR/.env" || true
  fi
}

# Preserve application code for rollback. Persistent state is snapshotted after
# the replacement image has built, during the short cutover window.
rsync -a --delete --exclude 'data/' --exclude '.env' --exclude '.git/' "$PROJECT_DIR/" "$ROLLBACK/"

write_status "validating" "$TARGET_VERSION" "Validating and extracting update"
python3 - "$PENDING" "$EXTRACT" <<'PY'
import os, sys, zipfile
src,dst=sys.argv[1:]
with zipfile.ZipFile(src) as z:
    infos=[i for i in z.infolist() if not i.is_dir()]
    for i in infos:
        name=i.filename.replace('\\','/').lstrip('/')
        parts=[p for p in name.split('/') if p not in ('','.')]
        if '..' in parts: raise SystemExit('unsafe ZIP path')
        target=os.path.realpath(os.path.join(dst,*parts))
        if not target.startswith(os.path.realpath(dst)+os.sep): raise SystemExit('unsafe ZIP target')
        os.makedirs(os.path.dirname(target),exist_ok=True)
        with z.open(i) as r, open(target,'wb') as w:
            while True:
                b=r.read(1024*1024)
                if not b: break
                w.write(b)
PY

# Accept either a single top-level version directory or a flat package.
SOURCE="$EXTRACT"
mapfile -t TOPDIRS < <(find "$EXTRACT" -mindepth 1 -maxdepth 1 -type d -printf '%p\n')
mapfile -t TOPFILES < <(find "$EXTRACT" -mindepth 1 -maxdepth 1 -type f -printf '%p\n')
if [[ ${#TOPDIRS[@]} -eq 1 && ${#TOPFILES[@]} -eq 0 && -f "${TOPDIRS[0]}/docker-compose.yml" ]]; then
  SOURCE="${TOPDIRS[0]}"
fi
for req in bot.py config.py docker-compose.yml upgrade-from-current.sh; do
  [[ -f "$SOURCE/$req" ]] || fail "update package missing $req"
done

write_status "installing" "$TARGET_VERSION" "Replacing application code"
# Never overwrite runtime secrets/state. --delete removes files deleted by the release.
rsync -a --delete --exclude 'data/' --exclude '.env' --exclude '.git/' "$SOURCE/" "$PROJECT_DIR/"
chmod +x "$PROJECT_DIR/upgrade-from-current.sh" "$PROJECT_DIR/install-managed-updater.sh" 2>/dev/null || true

cd "$PROJECT_DIR"
if ! docker compose config >/dev/null; then
  write_status "rolling-back" "$TARGET_VERSION" "Compose validation failed"
  rsync -a --delete --exclude 'data/' --exclude '.env' --exclude '.git/' "$ROLLBACK/" "$PROJECT_DIR/"
  docker compose up -d --build --remove-orphans || true
  fail "new docker-compose.yml did not validate"
fi

write_status "building" "$TARGET_VERSION" "Building replacement image while persistent data remains intact"
if ! docker compose build; then
  write_status "rolling-back" "$TARGET_VERSION" "Image build failed"
  rsync -a --delete --exclude 'data/' --exclude '.env' --exclude '.git/' "$ROLLBACK/" "$PROJECT_DIR/"
  docker compose up -d --build --remove-orphans || true
  fail "Docker image build failed; old code restored"
fi

write_status "snapshotting" "$TARGET_VERSION" "Stopping briefly and snapshotting persistent state"
# Build completed while the old container remained online. Stop only now so the
# SQLite databases and encrypted runtime files can be copied consistently.
docker compose stop >/dev/null
rsync -a --delete --exclude 'updates/' "$PROJECT_DIR/data/" "$DATA_ROLLBACK/"
if [[ -f "$PROJECT_DIR/.env" ]]; then cp -a "$PROJECT_DIR/.env" "$ENV_ROLLBACK"; fi

write_status "restarting" "$TARGET_VERSION" "Starting updated PBX"
if ! docker compose up -d --remove-orphans; then
  write_status "rolling-back" "$TARGET_VERSION" "Compose startup failed"
  rsync -a --delete --exclude 'data/' --exclude '.env' --exclude '.git/' "$ROLLBACK/" "$PROJECT_DIR/"
  restore_runtime_state
  docker compose up -d --build --remove-orphans || true
  rm -f "$APPLY"
  fail "updated stack failed to start; old code and persistent state restored"
fi

write_status "health-check" "$TARGET_VERSION" "Waiting for container health"
healthy=0
for _ in $(seq 1 45); do
  state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  if [[ "$state" == "healthy" ]]; then healthy=1; break; fi
  if [[ "$state" == "unhealthy" || "$state" == "exited" || "$state" == "dead" ]]; then break; fi
  sleep 2
done

if [[ "$healthy" -ne 1 ]]; then
  write_status "rolling-back" "$TARGET_VERSION" "Health check failed"
  docker compose down --remove-orphans || true
  rsync -a --delete --exclude 'data/' --exclude '.env' --exclude '.git/' "$ROLLBACK/" "$PROJECT_DIR/"
  restore_runtime_state
  docker compose up -d --build --remove-orphans || true
  rm -f "$APPLY"
  fail "updated container did not become healthy; old code and persistent state restored"
fi

# Refresh the installed updater binary for future releases without requiring
# another privileged installation step. Replacing the running executable is safe
# on Linux; this process continues on its existing inode.
if [[ -x "$PROJECT_DIR/updater/managed-update-agent.sh" && -w "/usr/local/sbin" ]]; then
  install -m 0755 "$PROJECT_DIR/updater/managed-update-agent.sh" /usr/local/sbin/discord-pbx-managed-update || true
fi
rm -f "$APPLY" "$PENDING" "$META"
write_status "healthy" "$TARGET_VERSION" "Update completed successfully at $STAMP"
echo "DiscordPBX update to $TARGET_VERSION completed successfully."
