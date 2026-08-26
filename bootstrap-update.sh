#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${DISCORDPBX_REPO:-p5zggfdnjx-design/discordpbx}"
CONTAINER_NAME="${DISCORDPBX_CONTAINER:-discord-pbx}"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run with sudo/root so the host updater can be repaired and started." >&2
  exit 1
fi
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

PROJECT_DIR="$(docker inspect "$CONTAINER_NAME" --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" && -d "$PROJECT_DIR" ]] || { echo "Could not determine the running DiscordPBX compose directory." >&2; exit 1; }
[[ -f "$PROJECT_DIR/docker-compose.yml" ]] || { echo "docker-compose.yml not found in $PROJECT_DIR" >&2; exit 1; }

if [[ -x "$PROJECT_DIR/install-managed-updater.sh" ]]; then
  "$PROJECT_DIR/install-managed-updater.sh"
else
  echo "Managed updater installer is missing from $PROJECT_DIR" >&2
  exit 1
fi

UPDATES_DIR="$PROJECT_DIR/data/updates"
mkdir -p "$UPDATES_DIR"
TMP="$UPDATES_DIR/pending.bootstrap"
FINAL="$UPDATES_DIR/pending.zip"
META="$UPDATES_DIR/pending_meta.json"
APPLY="$UPDATES_DIR/apply.json"
rm -f "$TMP"

echo "Checking latest DiscordPBX release from $REPO ..."
RELEASE_JSON="$(curl -fsSL -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$REPO/releases/latest")"
readarray -t RELEASE_INFO < <(python3 -c '
import json,sys
p=json.load(sys.stdin)
tag=str(p.get("tag_name") or "")
assets=p.get("assets") or []
zip_assets=[a for a in assets if str(a.get("name","")).lower().endswith(".zip") and not str(a.get("name","")).lower().endswith(".zip.sha256")]
preferred=[a for a in zip_assets if "discord-freepbx-bridge" in str(a.get("name","")).lower()]
a=(preferred or zip_assets or [None])[0]
if not tag or not a: raise SystemExit("Latest release does not contain a DiscordPBX ZIP asset")
print(tag)
print(a.get("name", "release.zip"))
print(a.get("browser_download_url", ""))
' <<<"$RELEASE_JSON")
TAG="${RELEASE_INFO[0]:-}"
ASSET_NAME="${RELEASE_INFO[1]:-}"
ASSET_URL="${RELEASE_INFO[2]:-}"
[[ -n "$ASSET_URL" ]] || { echo "Release ZIP URL is missing." >&2; exit 1; }

echo "Downloading $TAG ($ASSET_NAME) ..."
curl -fL --retry 3 --retry-delay 2 -o "$TMP" "$ASSET_URL"

PACKAGE_INFO="$(python3 - "$TMP" <<'PY'
import json,re,sys,zipfile
path=sys.argv[1]
required={"bot.py","config.py","docker-compose.yml","upgrade-from-current.sh"}
with zipfile.ZipFile(path) as z:
    names=[n.replace('\\','/').lstrip('/') for n in z.namelist() if not n.endswith('/')]
    candidates=[]
    for name in names:
        parts=[p for p in name.split('/') if p not in ('','.')]
        if '..' in parts: raise SystemExit('unsafe ZIP path')
        if parts: candidates.append(parts)
    prefixes={tuple(p[:-1]) for p in candidates if p[-1]=='docker-compose.yml'}
    selected=None
    for prefix in sorted(prefixes,key=len):
        base='/'.join(prefix)
        have={('/'.join(p)) for p in candidates}
        if all(((base+'/'+r) if base else r) in have for r in required):
            selected=(base+'/' if base else '')
            break
    if selected is None: raise SystemExit('ZIP is not a complete DiscordPBX release')
    cfg=z.read(selected+'config.py').decode('utf-8',errors='replace')
    m=re.search(r'version\s*=\s*["\x27]([^"\x27]+)',cfg)
    version=m.group(1).strip() if m else 'unknown'
    total=sum(i.file_size for i in z.infolist())
print(json.dumps({'version':version,'expanded_bytes':total}))
PY
)"
SHA="$(sha256sum "$TMP" | awk '{print $1}')"
BYTES="$(stat -c %s "$TMP")"
VERSION="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])' <<<"$PACKAGE_INFO")"
EXPANDED="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["expanded_bytes"])' <<<"$PACKAGE_INFO")"

mv -f "$TMP" "$FINAL"
python3 - "$META" "$ASSET_NAME" "$VERSION" "$SHA" "$BYTES" "$EXPANDED" "$REPO" "$TAG" <<'PY'
import json,os,sys,time
path,filename,version,sha,size,expanded,repo,tag=sys.argv[1:]
data={
  'filename':filename,'version':version,'sha256':sha,'bytes':int(size),
  'expanded_bytes':int(expanded),'uploaded_at':time.time(),
  'uploaded_by':'host recovery bootstrap','source':'github',
  'github_repo':repo,'github_tag':tag,
}
tmp=path+'.tmp'
with open(tmp,'w',encoding='utf-8') as f: json.dump(data,f,indent=2)
os.replace(tmp,path)
PY
python3 - "$APPLY" "$VERSION" "$SHA" <<'PY'
import json,os,sys,time
path,version,sha=sys.argv[1:]
data={'requested_at':time.time(),'requested_by':'host recovery bootstrap','requested_by_user_id':'host','current_version':'unknown','target_version':version,'sha256':sha,'agent_confirmed':True}
tmp=path+'.tmp'
with open(tmp,'w',encoding='utf-8') as f: json.dump(data,f,indent=2)
os.replace(tmp,path)
PY

PROJECT_UID="$(stat -c %u "$PROJECT_DIR")"
PROJECT_GID="$(stat -c %g "$PROJECT_DIR")"
chown "$PROJECT_UID:$PROJECT_GID" "$FINAL" "$META" "$APPLY" 2>/dev/null || true
chmod 0660 "$FINAL" "$META" "$APPLY" 2>/dev/null || true
systemctl start discord-pbx-update.path >/dev/null 2>&1 || true
systemctl start discord-pbx-update.service

echo "Queued DiscordPBX v$VERSION. The updater is now building/health-checking it."
echo "Status: $UPDATES_DIR/status.json"
