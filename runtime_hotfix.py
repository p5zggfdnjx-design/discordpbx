from __future__ import annotations

import json
import os
import time
from typing import Any


UPDATER_UI_SCRIPT = r'''<script id="pbx-updater-hotfix-v333">
(()=>{'use strict';
const q=s=>document.querySelector(s);
let updateState=null, githubState=null, busy=false, syncing=false;
function cookie(name){return document.cookie.split('; ').find(x=>x.startsWith(name+'='))?.split('=').slice(1).join('=')||''}
function esc(v){return String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
function notify(message,bad=false){const t=q('#toast');if(!t){console[bad?'error':'log'](message);return}t.textContent=message;t.className='toast show'+(bad?' error':'');setTimeout(()=>{if(t.textContent===message)t.className='toast'},4500)}
async function request(path,opt={}){const headers=new Headers(opt.headers||{});let body=opt.body;if(body&&typeof body==='object'&&!(body instanceof FormData)){headers.set('content-type','application/json');body=JSON.stringify(body)}if(!['GET','HEAD'].includes((opt.method||'GET').toUpperCase()))headers.set('X-CSRF-Token',decodeURIComponent(cookie('pbx_csrf')));const ctrl=new AbortController(),tm=setTimeout(()=>ctrl.abort(),opt.timeout||30000);try{const r=await fetch(path,{...opt,body,headers,signal:ctrl.signal,cache:'no-store',credentials:'same-origin'});if(r.status===401){location='/login';throw Error('Sign in required')}const ct=r.headers.get('content-type')||'',data=ct.includes('json')?await r.json():await r.text();if(!r.ok)throw Error(typeof data==='object'?(data.error||data.detail||`HTTP ${r.status}`):String(data||`HTTP ${r.status}`));return data}catch(e){if(e.name==='AbortError')throw Error('Request timed out');throw e}finally{clearTimeout(tm)}}
function syncButtons(){const apply=q('#updateApply'),manual=q('#updateNow'),gh=q('#updateGithubNow'),pending=updateState?.pending||{},applyPending=!!updateState?.apply_pending,hasGh=!!githubState?.latest?.asset?.name;if(apply){const want=busy||applyPending||!pending.version;if(apply.disabled!==want)apply.disabled=want}if(manual){const want=busy||applyPending;if(manual.disabled!==want)manual.disabled=want}if(gh){const want=busy||applyPending||!hasGh;if(gh.disabled!==want)gh.disabled=want}}
function renderUpdate(u){if(!u)return;updateState=u;const agent=u.agent||{},pending=u.pending||{},confirmed=!!u.agent_confirmed||!!(agent.managed&&agent.project_dir),queue=!!u.queue_writable;const host=confirmed?'Ready':(queue?'Queue ready; host watcher not yet confirmed':'Unavailable');const hostClass=confirmed?'goodText':(queue?'amberText':'dangerText');const box=q('#updateStatus');if(box)box.innerHTML=`<div class="listrow"><b>Current</b><span>v${esc(u.current_version||'?')}</span></div><div class="listrow"><b>Host updater</b><span class="${hostClass}">${host}</span></div>${agent.state?`<div class="listrow"><b>Updater state</b><span>${esc(agent.state)}${agent.version?' · v'+esc(agent.version):''}</span></div>`:''}`;const p=q('#updatePending');if(p)p.textContent=pending.version?`Staged: v${pending.version} · ${pending.filename||'package'} · ${Math.round((pending.bytes||0)/1024)} KB${u.apply_pending?' · install queued':''}`:'No update staged.';syncButtons()}
function renderGithub(g){if(!g)return;githubState=g;const box=q('#updateGithubStatus'),repo=q('#updateGithubRepo');if(repo&&document.activeElement!==repo)repo.value=g.repo||repo.value||'';if(box){if(!g.configured)box.textContent='Set a GitHub repository in owner/repository format.';else if(g.error)box.innerHTML=`<span class="dangerText">${esc(g.error)}</span>`;else{const latest=g.latest||{},asset=latest.asset||{};box.innerHTML=`Repository <b>${esc(g.repo||'')}</b> · latest <b>${esc(latest.tag||'?')}</b> · ${asset.name?esc(asset.name):'no ZIP'} · ${latest.newer?'<span class="goodText">update available</span>':'current/newer already installed'}`}}syncButtons()}
async function refreshUpdater(showErrors=false){if(syncing)return;syncing=true;try{const [u,g]=await Promise.allSettled([request('/api/system/update/status'),request('/api/system/update/github')]);if(u.status==='fulfilled')renderUpdate(u.value);else if(showErrors)notify('Updater status: '+u.reason.message,true);if(g.status==='fulfilled')renderGithub(g.value);else if(showErrors)notify('GitHub check: '+g.reason.message,true)}finally{syncing=false;syncButtons()}}
async function uploadSelected(){const input=q('#updateFile'),file=input?.files?.[0];if(!file)throw Error('Choose a DiscordPBX ZIP first');const fd=new FormData();fd.append('file',file,file.name);const j=await request('/api/system/update/upload',{method:'POST',body:fd,timeout:120000});if(input)input.value='';if(j.pending)renderUpdate({...updateState,pending:j.pending,apply_pending:false});return j}
async function queueInstall(){const j=await request('/api/system/update/apply',{method:'POST',body:{},timeout:30000});updateState={...(updateState||{}),apply_pending:true};renderUpdate(updateState);notify(j.message||'Update queued');waitForVersion(j.target_version);return j}
async function waitForVersion(target){const start=Date.now();while(Date.now()-start<240000){await new Promise(r=>setTimeout(r,3500));try{const r=await fetch('/api/setup/status',{cache:'no-store'});if(!r.ok)continue;const j=await r.json();if(j.version&&(!target||j.version===target)){location.reload();return}}catch(_){}}notify('Update is still running. Refresh in a moment if this page does not reload.',true)}
function ownClick(id,handler){const el=q(id);if(!el)return;el.onclick=async e=>{e.preventDefault();e.stopPropagation();if(busy)return;busy=true;syncButtons();try{await handler(el)}catch(err){notify(err.message||String(err),true)}finally{busy=false;await refreshUpdater(false);syncButtons()}}}
function installHandlers(){
ownClick('#updateRefresh',async()=>refreshUpdater(true));
ownClick('#updateGithubCheck',async()=>{const g=await request('/api/system/update/github',{timeout:30000});renderGithub(g);notify(g.error?'GitHub check needs attention':'GitHub release checked',!!g.error)});
ownClick('#updateStage',async()=>{const j=await uploadSelected();notify(j.message||'Update staged')});
ownClick('#updateApply',async()=>{if(!confirm('Install the staged DiscordPBX update now? Active calls may be interrupted during the restart.'))return;await queueInstall()});
ownClick('#updateNow',async()=>{if(!confirm('Install the selected or staged DiscordPBX release now? Active calls will be interrupted during the short cutover.'))return;if(q('#updateFile')?.files?.[0])await uploadSelected();if(!(updateState?.pending?.version)){const u=await request('/api/system/update/status');renderUpdate(u)}if(!updateState?.pending?.version)throw Error('Choose a release ZIP first, or stage one before pressing Update');await queueInstall()});
ownClick('#updateGithubNow',async()=>{if(!confirm('Download and install the latest GitHub release now? Active calls will be interrupted during the short cutover.'))return;const j=await request('/api/system/update/github/install',{method:'POST',body:{},timeout:150000});updateState={...(updateState||{}),pending:{...((updateState||{}).pending||{}),version:j.target_version},apply_pending:true};renderUpdate(updateState);notify(j.message||'GitHub update queued');waitForVersion(j.target_version)});
const watched=[q('#updateApply'),q('#updateNow'),q('#updateGithubNow')].filter(Boolean);if(watched.length){const mo=new MutationObserver(()=>queueMicrotask(syncButtons));watched.forEach(x=>mo.observe(x,{attributes:true,attributeFilter:['disabled']}))}
refreshUpdater(false);setInterval(()=>{if(q('#settings')?.classList.contains('active'))refreshUpdater(false)},3000)
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',installHandlers,{once:true});else installHandlers();
})();
</script>'''


def inject_updater_ui(html: str) -> str:
    """Inject the resilient updater controller exactly once."""
    if 'id="pbx-updater-hotfix-v333"' in html:
        return html
    if "</body>" in html:
        return html.replace("</body>", UPDATER_UI_SCRIPT + "</body>", 1)
    return html + UPDATER_UI_SCRIPT


def _agent_confirmed(agent: dict[str, Any]) -> bool:
    return bool(agent.get("managed") and agent.get("project_dir"))


def _queue_update(self, actor: dict[str, Any]) -> dict[str, Any]:
    """Queue a staged update without treating a stale status.json as a hard failure.

    The host systemd path unit is triggered by apply.json. status.json is useful
    telemetry, but it can be missing after a migration or ownership repair even
    when the path unit is installed and working.
    """
    pending = self._updates_dir / "pending.zip"
    meta = self._read_update_json(self._updates_dir / "pending_meta.json", {})
    if not pending.exists() or not meta:
        raise ValueError("stage an update first")
    if not os.access(self._updates_dir, os.W_OK):
        raise RuntimeError("update queue is not writable by the PBX container")

    agent = self._read_update_json(self._update_status_path(), {})
    marker = {
        "requested_at": time.time(),
        "requested_by": actor.get("name", "system admin"),
        "requested_by_user_id": actor.get("user_id", ""),
        "current_version": self.config.version,
        "target_version": meta.get("version", "unknown"),
        "sha256": meta.get("sha256", ""),
        "agent_confirmed": _agent_confirmed(agent),
    }
    tmp = self._updates_dir / "apply.json.tmp"
    tmp.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    os.replace(tmp, self._updates_dir / "apply.json")
    self.db.audit(
        "system.update.requested",
        actor_user_id=actor["user_id"],
        actor_name=actor["name"],
        auth_type=actor.get("auth_type", "session"),
        entity_type="system",
        entity_id=str(meta.get("version", "")),
        detail={
            "sha256": meta.get("sha256", ""),
            "source": meta.get("source", "upload"),
            "agent_confirmed": _agent_confirmed(agent),
        },
    )
    return meta


async def _system_update_status(self, request):
    from aiohttp import web

    await self._system_admin(request)
    pending_meta = self._read_update_json(self._updates_dir / "pending_meta.json", {})
    agent = self._read_update_json(self._update_status_path(), {})
    queue_writable = self._updates_dir.exists() and os.access(self._updates_dir, os.W_OK)
    confirmed = _agent_confirmed(agent)
    return web.json_response(
        {
            "ok": True,
            "current_version": self.config.version,
            "pending": pending_meta,
            "apply_pending": (self._updates_dir / "apply.json").exists(),
            "agent": agent,
            "agent_confirmed": confirmed,
            "queue_writable": queue_writable,
            "managed_agent_ready": bool(confirmed or queue_writable),
        }
    )


def apply() -> None:
    """Apply updater reliability fixes before the PBX web server is instantiated."""
    import webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_v333_updater_hotfix_applied", False):
        return

    original_index = cls.index

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "content_type", "") == "text/html" and response.text:
                response.text = inject_updater_ui(response.text)
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
                response.headers["Pragma"] = "no-cache"
        except Exception:
            pass
        return response

    cls.index = index
    cls._queue_update = _queue_update
    cls.system_update_status = _system_update_status
    cls._v333_updater_hotfix_applied = True
