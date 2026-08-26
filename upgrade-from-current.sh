#!/usr/bin/env bash
set -euo pipefail

NEW_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER_NAME="${PBX_CONTAINER_NAME:-discord-pbx}"
OLD_DIR="${PBX_OLD_DIR:-}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed or not in PATH" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 is not available (docker compose)." >&2
  exit 1
fi

if [[ -z "$OLD_DIR" ]] && docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  OLD_DIR="$(docker inspect "$CONTAINER_NAME" --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' 2>/dev/null || true)"
fi

if [[ -n "$OLD_DIR" && ! -d "$OLD_DIR" ]]; then
  echo "ERROR: previous project directory does not exist: $OLD_DIR" >&2
  exit 1
fi

if [[ -n "$OLD_DIR" && "$OLD_DIR" != "$NEW_DIR" ]]; then
  echo "Migrating persistent configuration from: $OLD_DIR"
  if [[ -f "$OLD_DIR/.env" && ! -f "$NEW_DIR/.env" ]]; then
    cp -p "$OLD_DIR/.env" "$NEW_DIR/.env"
  fi
  mkdir -p "$NEW_DIR/data"
  if [[ -d "$OLD_DIR/data" ]]; then
    cp -a "$OLD_DIR/data/." "$NEW_DIR/data/"
  fi
elif [[ -n "$OLD_DIR" && "$OLD_DIR" == "$NEW_DIR" ]]; then
  echo "Current container already points at this project directory."
else
  echo "No existing $CONTAINER_NAME container was found; treating this as a fresh install."
  echo "If your old project still exists but its container was removed, rerun with:"
  echo "  PBX_OLD_DIR=/path/to/old/project ./upgrade-from-current.sh"
fi

if [[ ! -f "$NEW_DIR/.env" ]]; then
  echo "Creating .env from .env.example. Finish setup from the web wizard after startup."
  cp "$NEW_DIR/.env.example" "$NEW_DIR/.env"
fi

mkdir -p "$NEW_DIR/data"
cd "$NEW_DIR"

# Validate and build BEFORE stopping the working container. A build failure leaves
# the current PBX untouched and minimizes service downtime during the cutover.
echo "Validating Compose configuration..."
docker compose config >/dev/null
echo "Building v3 image while the current PBX remains online..."
docker compose build

echo "Replacing $CONTAINER_NAME..."
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker compose up -d

# Wait for the container healthcheck. If startup fails, restore the previous
# Compose project when we know where it is.
echo "Waiting for PBX healthcheck..."
healthy=0
for _ in $(seq 1 35); do
  state="$(docker inspect "$CONTAINER_NAME" --format '{{.State.Status}}' 2>/dev/null || true)"
  health="$(docker inspect "$CONTAINER_NAME" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
  if [[ "$state" == "running" && "$health" == "healthy" ]]; then
    healthy=1
    break
  fi
  if [[ "$state" == "running" && -z "$health" ]]; then
    healthy=1
    break
  fi
  if [[ "$state" == "exited" || "$state" == "dead" || "$health" == "unhealthy" ]]; then
    break
  fi
  sleep 2
done

if [[ "$healthy" -ne 1 ]]; then
  echo "ERROR: v3 did not become healthy. Recent logs:" >&2
  docker compose logs --tail=120 >&2 || true
  if [[ -n "$OLD_DIR" && "$OLD_DIR" != "$NEW_DIR" && -f "$OLD_DIR/docker-compose.yml" ]]; then
    echo "Attempting automatic rollback to: $OLD_DIR" >&2
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    (cd "$OLD_DIR" && docker compose up -d) || true
  fi
  exit 1
fi

echo
docker compose ps
echo
echo "PBX v3.2.9 is healthy. Startup log tail:"
docker compose logs --tail=80
