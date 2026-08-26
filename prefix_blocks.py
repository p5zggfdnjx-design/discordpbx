from __future__ import annotations

import json
import random
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import yaml


_BLOCK_RE = re.compile(r"(?<!\d)(?:1[\s().-]*)?([2-9]\d{2})[\s.-]*([2-9]\d{2})(?!\d)")


def normalize_prefix(raw: str) -> str:
    """Normalize a NANP NPA-NXX block to six digits (for example 407200)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 7 and digits.startswith("1"):
        digits = digits[1:]
    if not re.fullmatch(r"[2-9]\d{2}[2-9]\d{2}", digits):
        raise ValueError("block must be a valid 6-digit NANP NPA-NXX prefix, such as 407200")
    return digits


def extract_bulk_prefixes(raw: str) -> tuple[list[str], list[str]]:
    text = str(raw or "")
    matches = list(_BLOCK_RE.finditer(text))
    valid: list[str] = []
    seen: set[str] = set()
    for match in matches:
        prefix = match.group(1) + match.group(2)
        if prefix not in seen:
            seen.add(prefix)
            valid.append(prefix)

    chars = list(text)
    for match in matches:
        for i in range(match.start(), match.end()):
            chars[i] = " "
    residual = "".join(chars)
    invalid: list[str] = []
    invalid_seen: set[str] = set()
    for token in re.split(r"[\n,;\t ]+", residual):
        token = token.strip()
        if not token or not any(ch.isdigit() for ch in token):
            continue
        try:
            prefix = normalize_prefix(token)
        except ValueError:
            if token not in invalid_seen:
                invalid_seen.add(token)
                invalid.append(token)
        else:
            if prefix not in seen:
                seen.add(prefix)
                valid.append(prefix)
    return sorted(valid), invalid


class PrefixBlockStore:
    """Small persistent allowlist of 10,000-number NANP NPA-NXX blocks."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        self._mtime_ns = -1
        if self.path.exists():
            self._items = self._read_file()
            self._commit(self._items)
        else:
            self._commit([])

    @staticmethod
    def _new_entry(prefix: str) -> dict[str, Any]:
        return {"id": uuid.uuid4().hex, "prefix": normalize_prefix(prefix), "enabled": True}

    @staticmethod
    def _normalize_items(items) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in items or []:
            if isinstance(raw, str):
                raw = {"prefix": raw}
            if not isinstance(raw, dict):
                continue
            try:
                prefix = normalize_prefix(raw.get("prefix", ""))
            except ValueError:
                continue
            if prefix in seen:
                continue
            seen.add(prefix)
            out.append({
                "id": str(raw.get("id") or uuid.uuid4().hex),
                "prefix": prefix,
                "enabled": bool(raw.get("enabled", True)),
            })
        out.sort(key=lambda x: x["prefix"])
        return out

    def _read_file(self) -> list[dict[str, Any]]:
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            payload = {}
        raw = payload.get("blocks", []) if isinstance(payload, dict) else []
        return self._normalize_items(raw if isinstance(raw, list) else [])

    def _remember_mtime(self) -> None:
        try:
            self._mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime_ns = -1

    def _refresh(self) -> None:
        try:
            current = self.path.stat().st_mtime_ns
        except OSError:
            return
        if current != self._mtime_ns:
            self._items = self._read_file()
            self._mtime_ns = current

    def _commit(self, items) -> None:
        normalized = self._normalize_items(items)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump({"blocks": normalized}, sort_keys=False), encoding="utf-8")
        tmp.replace(self.path)
        self._items = normalized
        self._remember_mtime()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            return [dict(x) for x in self._items]

    def enabled_prefixes(self) -> list[str]:
        with self._lock:
            self._refresh()
            return [x["prefix"] for x in self._items if x.get("enabled")]

    def counts(self) -> tuple[int, int]:
        with self._lock:
            self._refresh()
            return len(self._items), sum(1 for x in self._items if x.get("enabled"))

    def add_bulk(self, raw: str) -> dict[str, Any]:
        prefixes, invalid = extract_bulk_prefixes(raw)
        with self._lock:
            self._refresh()
            existing = {x["prefix"] for x in self._items}
            duplicates = [p for p in prefixes if p in existing]
            addable = [p for p in prefixes if p not in existing]
            self._commit([*self._items, *(self._new_entry(p) for p in addable)])
        return {"valid": prefixes, "addable": addable, "duplicates": duplicates, "invalid": invalid, "added": len(addable)}

    def remove_bulk(self, raw: str) -> dict[str, Any]:
        prefixes, invalid = extract_bulk_prefixes(raw)
        requested = set(prefixes)
        with self._lock:
            self._refresh()
            existing = {x["prefix"] for x in self._items}
            removed = sorted(requested & existing)
            missing = sorted(requested - existing)
            if removed:
                self._commit([x for x in self._items if x["prefix"] not in requested])
        return {"valid": prefixes, "removed_prefixes": removed, "missing": missing, "invalid": invalid, "removed": len(removed)}

    def random_number(self, prefix: str | None = None) -> str:
        if prefix is None:
            enabled = self.enabled_prefixes()
            if not enabled:
                raise ValueError("no enabled number blocks are configured")
            prefix = random.choice(enabled)
        prefix = normalize_prefix(prefix)
        return "1" + prefix + f"{random.randrange(10000):04d}"


def _json_response_payload(response) -> dict[str, Any] | None:
    try:
        payload = json.loads(response.text)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _inject_ui(html: str) -> str:
    if 'id="pbx-prefix-blocks-script"' in html:
        return html
    addon = r'''<style id="pbx-prefix-blocks-style">
.blockPanel{margin-top:16px;padding-top:14px;border-top:1px solid var(--border)}
.blockGrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:7px;margin-top:9px}
.blockChip{display:flex;align-items:center;justify-content:space-between;gap:7px;padding:8px 10px;border:1px solid var(--border);border-radius:10px;background:var(--panel3)}
.blockChip code{color:#8dffb7;font-weight:800}.blockChip button{min-height:32px;padding:4px 8px}
</style>
<script id="pbx-prefix-blocks-script">
(()=>{'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
function cookie(name){return document.cookie.split('; ').find(x=>x.startsWith(name+'='))?.split('=').slice(1).join('=')||''}
function flash(msg,bad=false){const t=$('#toast');if(!t){console[bad?'error':'log'](msg);return}t.textContent=msg;t.className='toast show'+(bad?' error':'');setTimeout(()=>{if(t.textContent===msg)t.className='toast'},4500)}
async function req(path,opt={}){const h=new Headers(opt.headers||{});let body=opt.body;if(body&&typeof body==='object'){h.set('content-type','application/json');body=JSON.stringify(body)}if(!['GET','HEAD'].includes((opt.method||'GET').toUpperCase()))h.set('X-CSRF-Token',decodeURIComponent(cookie('pbx_csrf')));const r=await fetch(path,{...opt,headers:h,body,cache:'no-store',credentials:'same-origin'});const ct=r.headers.get('content-type')||'',j=ct.includes('json')?await r.json():await r.text();if(!r.ok)throw Error(typeof j==='object'?(j.error||`HTTP ${r.status}`):j);return j}
function panel(kind){const isCid=kind==='cid',host=$(isCid?'#cidSub section':'#randomSub section');if(!host||$(`#${kind}BlockPanel`))return;const box=document.createElement('div');box.id=`${kind}BlockPanel`;box.className='blockPanel';box.innerHTML=`<div class="row"><div><h3 style="margin:0">${isCid?'Caller ID blocks':'Random destination blocks'}</h3><div class="muted small">${isCid?'Owned/verified NPA-NXX blocks only. Random Caller ID selects a 6-digit block first, then generates the final four digits.':'The Random button selects one configured NPA-NXX block and generates the final four digits. One call is placed per press; normal DNC and rate limits still apply.'}</div></div><span id="${kind}BlockCount" class="tag" style="margin-left:auto">0 blocks</span></div><textarea id="${kind}BlockBulk" style="margin-top:9px" placeholder="407200\n352201\n407-202"></textarea><div class="row"><button class="btn primary" id="${kind}BlockAdd">Bulk Add Blocks</button><button class="btn danger" id="${kind}BlockRemove">Bulk Remove Blocks</button></div><div id="${kind}BlockList" class="blockGrid"></div>`;host.appendChild(box);$(`#${kind}BlockAdd`).onclick=()=>mutate(kind,'add');$(`#${kind}BlockRemove`).onclick=()=>mutate(kind,'remove')}
async function load(kind){panel(kind);const isCid=kind==='cid',path=isCid?'/api/caller-id-pool':'/api/random-call-pool';try{const j=await req(path+'?limit=100&include_blocks=1'),rows=j.blocks||[];const c=$(`#${kind}BlockCount`);if(c)c.textContent=`${j.block_enabled_count??rows.length} enabled / ${j.block_total_count??rows.length} blocks`;const list=$(`#${kind}BlockList`);if(list)list.innerHTML=rows.length?rows.map(x=>`<div class="blockChip"><div><code>${x.prefix.slice(0,3)}-${x.prefix.slice(3)}</code><div class="muted small">10,000 numbers</div></div><button class="btn danger" data-${kind}-block-del="${x.prefix}">Remove</button></div>`).join(''):'<div class="muted small">No blocks configured.</div>';$$(`[data-${kind}-block-del]`).forEach(b=>b.onclick=()=>mutate(kind,'remove',b.getAttribute(`data-${kind}-block-del`)))}catch(e){flash('Could not load number blocks: '+e.message,true)}}
async function mutate(kind,action,direct=''){const isCid=kind==='cid',input=$(`#${kind}BlockBulk`),text=direct||(input?.value||'');if(!text.trim())return;try{const path=isCid?'/api/caller-id-pool/bulk?mode=blocks':'/api/random-call-pool/bulk?mode=blocks',j=await req(path,{method:'POST',body:{text,action}});if(input&&!direct)input.value='';flash(j.message||`${action==='remove'?'Removed':'Added'} ${action==='remove'?j.removed:j.added} block(s)`);await load(kind)}catch(e){flash(e.message,true)}}
function init(){panel('cid');panel('random');$$('[data-sub="cid"]').forEach(b=>b.addEventListener('click',()=>setTimeout(()=>load('cid'),0)));$$('[data-sub="random"]').forEach(b=>b.addEventListener('click',()=>setTimeout(()=>load('random'),0)))}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();
</script>'''
    return html.replace("</body>", addon + "</body>", 1) if "</body>" in html else html + addon


def _fix_updater_freeze() -> None:
    """Remove the v3.3.6 self-triggering disabled-attribute observer."""
    try:
        import runtime_hotfix
    except Exception:
        return
    bad = "const watched=[q('#updateApply'),q('#updateNow'),q('#updateGithubCheck')].filter(Boolean);if(watched.length){const mo=new MutationObserver(()=>queueMicrotask(syncButtons));watched.forEach(x=>mo.observe(x,{attributes:true,attributeFilter:['disabled']}))}"
    if bad in runtime_hotfix.UPDATER_UI_SCRIPT:
        runtime_hotfix.UPDATER_UI_SCRIPT = runtime_hotfix.UPDATER_UI_SCRIPT.replace(bad, "")


def apply() -> None:
    """Add NPA-NXX block pools to the existing v3 APIs and operator UI."""
    import webui_v3
    from aiohttp import web

    _fix_updater_freeze()
    cls = webui_v3.WebControlServer
    if getattr(cls, "_prefix_blocks_applied", False):
        return

    original_init = cls.__init__
    original_index = cls.index
    original_status = cls.status
    original_cid_list = cls.caller_id_pool_list_v3
    original_random_list = cls.random_call_pool_list_v3
    original_cid_bulk = cls.caller_id_pool_bulk_v3
    original_random_bulk = cls.random_call_pool_bulk_v3
    original_choose_cid = cls._choose_web_caller_id
    original_choose_random = cls._choose_random_call_target
    original_authorized_cids = cls._authorized_caller_id_pool

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        data_dir = Path(self.config.data_dir)
        self.caller_id_blocks = PrefixBlockStore(str(data_dir / "caller_id_blocks.yaml"))
        self.random_call_blocks = PrefixBlockStore(str(data_dir / "random_call_blocks.yaml"))

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "content_type", "") == "text/html" and response.text:
                response.text = _inject_ui(response.text)
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        except Exception:
            pass
        return response

    async def status(self, request):
        response = await original_status(self, request)
        payload = _json_response_payload(response)
        if payload is None:
            return response
        cid_total, cid_enabled = self.caller_id_blocks.counts()
        random_total, random_enabled = self.random_call_blocks.counts()
        payload["caller_id_block_count"] = cid_total
        payload["caller_id_block_enabled_count"] = cid_enabled
        payload["random_call_block_count"] = random_total
        payload["random_call_block_enabled_count"] = random_enabled
        if cid_enabled:
            payload["random_caller_id_available"] = True
        if random_enabled:
            payload["random_call_pool_available_count"] = max(1, int(payload.get("random_call_pool_available_count", 0) or 0))
            used = int(payload.get("call_count", 0) or 0) + int(payload.get("outbound_pending_count", 0) or 0)
            maximum = int(payload.get("max_simultaneous_calls", self.config.max_simultaneous_calls) or self.config.max_simultaneous_calls)
            payload["random_call_available"] = used < maximum
        return web.json_response(payload, status=response.status)

    def authorized_cids(self):
        values = list(original_authorized_cids(self))
        for prefix in self.caller_id_blocks.enabled_prefixes():
            values.append(self.caller_id_blocks.random_number(prefix))
        return values

    def choose_cid(self, raw: str, randomize: bool = False) -> str:
        if not randomize:
            return original_choose_cid(self, raw, False)
        explicit = list(self.caller_id_pool.enabled_numbers())
        prefixes = self.caller_id_blocks.enabled_prefixes()
        if not explicit and not prefixes:
            raise ValueError("Random Caller ID needs an enabled full number or owned/verified NPA-NXX block")
        busy = self._busy_caller_ids()
        sources: list[tuple[str, str]] = [("number", n) for n in explicit] + [("block", p) for p in prefixes]
        random.shuffle(sources)
        for kind, value in sources:
            if kind == "number":
                candidate = value
                if candidate in busy and len(sources) > 1:
                    continue
                if candidate == self._last_random_caller_id and len(sources) > 1:
                    continue
            else:
                candidate = ""
                for _ in range(32):
                    test = self.caller_id_blocks.random_number(value)
                    if test not in busy and test != self._last_random_caller_id:
                        candidate = test
                        break
                if not candidate:
                    candidate = self.caller_id_blocks.random_number(value)
            self._last_random_caller_id = candidate
            return candidate
        return original_choose_cid(self, raw, True)

    def choose_random_target(self):
        busy = self._busy_destination_numbers()
        explicit = [x for x in self.random_call_pool.enabled() if str(x.get("number", "")) not in busy]
        prefixes = self.random_call_blocks.enabled_prefixes()
        sources: list[tuple[str, Any]] = [("number", x) for x in explicit] + [("block", p) for p in prefixes]
        if not sources:
            raise ValueError("Random Call needs an enabled destination number or NPA-NXX block")
        random.shuffle(sources)
        for kind, value in sources:
            if kind == "number":
                candidate = str(value.get("number", ""))
                if candidate == self._last_random_call_number and len(sources) > 1:
                    continue
                picked = dict(value)
            else:
                candidate = ""
                for _ in range(64):
                    test = self.random_call_blocks.random_number(str(value))
                    if test in busy or test == self._last_random_call_number:
                        continue
                    try:
                        if hasattr(self, "db") and self.db.dnc_contains(test):
                            continue
                    except Exception:
                        pass
                    candidate = test
                    break
                if not candidate:
                    continue
                picked = {
                    "id": f"block:{value}",
                    "number": candidate,
                    "label": f"Random block {str(value)[:3]}-{str(value)[3:]}",
                    "enabled": True,
                    "generated_from_block": str(value),
                }
            self._last_random_call_number = candidate
            return picked
        if explicit:
            return original_choose_random(self)
        raise ValueError("Could not generate an available destination from the configured blocks")

    async def cid_list(self, request):
        response = await original_cid_list(self, request)
        payload = _json_response_payload(response)
        if payload is None:
            return response
        rows = self.caller_id_blocks.list()
        total, enabled = self.caller_id_blocks.counts()
        payload.update({"blocks": rows, "block_total_count": total, "block_enabled_count": enabled})
        return web.json_response(payload, status=response.status)

    async def random_list(self, request):
        response = await original_random_list(self, request)
        payload = _json_response_payload(response)
        if payload is None:
            return response
        rows = self.random_call_blocks.list()
        total, enabled = self.random_call_blocks.counts()
        payload.update({"blocks": rows, "block_total_count": total, "block_enabled_count": enabled})
        return web.json_response(payload, status=response.status)

    async def cid_bulk(self, request):
        if request.query.get("mode") != "blocks":
            return await original_cid_bulk(self, request)
        await self._system_admin(request)
        try:
            body = await request.json()
            text = str(body.get("text", ""))
            action = str(body.get("action", "add")).lower()
            if action not in {"add", "remove"}:
                raise ValueError("block action must be add or remove")
            result = self.caller_id_blocks.add_bulk(text) if action == "add" else self.caller_id_blocks.remove_bulk(text)
            count = int(result.get("added" if action == "add" else "removed", 0))
            verb = "Added" if action == "add" else "Removed"
            return web.json_response({"ok": True, **result, "message": f"{verb} {count} caller-ID block(s)."})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def random_bulk(self, request):
        if request.query.get("mode") != "blocks":
            return await original_random_bulk(self, request)
        await self._system_admin(request)
        try:
            body = await request.json()
            text = str(body.get("text", ""))
            action = str(body.get("action", "add")).lower()
            if action not in {"add", "remove"}:
                raise ValueError("block action must be add or remove")
            result = self.random_call_blocks.add_bulk(text) if action == "add" else self.random_call_blocks.remove_bulk(text)
            count = int(result.get("added" if action == "add" else "removed", 0))
            verb = "Added" if action == "add" else "Removed"
            return web.json_response({"ok": True, **result, "message": f"{verb} {count} random-destination block(s)."})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    cls.__init__ = init
    cls.index = index
    cls.status = status
    cls._authorized_caller_id_pool = authorized_cids
    cls._choose_web_caller_id = choose_cid
    cls._choose_random_call_target = choose_random_target
    cls.caller_id_pool_list_v3 = cid_list
    cls.random_call_pool_list_v3 = random_list
    cls.caller_id_pool_bulk_v3 = cid_bulk
    cls.random_call_pool_bulk_v3 = random_bulk
    cls._prefix_blocks_applied = True
