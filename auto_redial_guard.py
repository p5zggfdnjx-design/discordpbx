from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from aiohttp import web

log = logging.getLogger("discord-pbx.auto-redial")

ACTIVE_STATES = {"armed", "waiting", "starting", "dialing", "screening", "paused"}
TERMINAL_STATES = {"answered", "stopped", "exhausted"}
RETRY_REASONS = {"no answer", "timeout", "failed", "busy", "voicemail", "disconnected"}
REDIAL_UI_MARKER = "pbx-redial-jobs-v2"
MAX_QUEUE_FAILURES = 10


def _now() -> float:
    return time.time()


def _normalize_reason(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if text in {"noanswer", "not answered", "ring timeout", "ringing timeout"}:
        return "no answer"
    if "busy" in text:
        return "busy"
    if "voicemail" in text or "machine" in text:
        return "voicemail"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if text in {"disconnect", "disconnected", "dropped"}:
        return "disconnected"
    if text in {"fail", "failed", "error"}:
        return "failed"
    if text in {"human", "answered", "connected", "completed"}:
        return "answered"
    return text


def _ensure_schema(server) -> None:
    # executescript() may implicitly commit, so keep DDL out of AppDatabase.transaction().
    with server.db._lock, server.db._connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS redial_jobs (
                job_id TEXT PRIMARY KEY,
                root_uuid TEXT NOT NULL UNIQUE,
                workspace_id TEXT NOT NULL DEFAULT '',
                number TEXT NOT NULL,
                caller_id TEXT NOT NULL DEFAULT '',
                contact_name TEXT NOT NULL DEFAULT '',
                randomize_caller_id INTEGER NOT NULL DEFAULT 0,
                interval_seconds REAL NOT NULL DEFAULT 5,
                max_attempts INTEGER NOT NULL DEFAULT 10,
                attempts_made INTEGER NOT NULL DEFAULT 0,
                queue_failures INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'armed',
                next_attempt_at REAL NOT NULL DEFAULT 0,
                current_call_uuid TEXT NOT NULL DEFAULT '',
                last_reason TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                operator_user_id TEXT NOT NULL DEFAULT '',
                operator_name TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_redial_jobs_workspace_state
                ON redial_jobs(workspace_id, state, next_attempt_at);
            CREATE INDEX IF NOT EXISTS idx_redial_jobs_current_call
                ON redial_jobs(current_call_uuid);

            CREATE TABLE IF NOT EXISTS redial_attempts (
                call_uuid TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES redial_jobs(job_id) ON DELETE CASCADE,
                attempt_no INTEGER NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_redial_attempts_job
                ON redial_attempts(job_id, attempt_no);
            """
        )


def _rowdict(row) -> dict[str, Any] | None:
    return dict(row) if row else None


def _job_by_root(server, root_uuid: str) -> dict[str, Any] | None:
    _ensure_schema(server)
    with server.db._lock, server.db._connect() as con:
        row = con.execute("SELECT * FROM redial_jobs WHERE root_uuid=?", (str(root_uuid),)).fetchone()
    return _rowdict(row)


def _job_by_call(server, call_uuid: str) -> dict[str, Any] | None:
    _ensure_schema(server)
    with server.db._lock, server.db._connect() as con:
        row = con.execute(
            """
            SELECT j.*
            FROM redial_jobs j
            LEFT JOIN redial_attempts a ON a.job_id=j.job_id
            WHERE j.root_uuid=? OR j.current_call_uuid=? OR a.call_uuid=?
            ORDER BY j.created_at DESC
            LIMIT 1
            """,
            (str(call_uuid), str(call_uuid), str(call_uuid)),
        ).fetchone()
    return _rowdict(row)


def _list_jobs(server, workspace_ids: set[str] | None = None, include_terminal: bool = False) -> list[dict[str, Any]]:
    _ensure_schema(server)
    sql = "SELECT * FROM redial_jobs"
    args: list[Any] = []
    clauses: list[str] = []
    if not include_terminal:
        clauses.append("state NOT IN ('answered','stopped','exhausted')")
    if workspace_ids is not None:
        ids = sorted(str(x) for x in workspace_ids if str(x))
        if not ids:
            return []
        clauses.append("workspace_id IN (%s)" % ",".join("?" for _ in ids))
        args.extend(ids)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC"
    with server.db._lock, server.db._connect() as con:
        rows = con.execute(sql, tuple(args)).fetchall()
    return [dict(r) for r in rows]


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"], "root_uuid": job["root_uuid"],
        "workspace_id": job["workspace_id"], "number": job["number"],
        "caller_id": job["caller_id"], "contact_name": job["contact_name"],
        "randomize_caller_id": bool(job["randomize_caller_id"]),
        "interval_seconds": float(job["interval_seconds"]),
        "max_attempts": int(job["max_attempts"]), "attempts_made": int(job["attempts_made"]),
        "state": job["state"], "next_attempt_at": float(job["next_attempt_at"]),
        "current_call_uuid": job["current_call_uuid"], "last_reason": job["last_reason"],
        "last_error": job["last_error"], "created_at": float(job["created_at"]),
        "updated_at": float(job["updated_at"]),
    }


def _insert_attempt(server, job_id: str, call_uuid: str, attempt_no: int) -> None:
    with server.db.transaction() as con:
        con.execute(
            "INSERT OR IGNORE INTO redial_attempts(call_uuid,job_id,attempt_no,started_at) VALUES(?,?,?,?)",
            (str(call_uuid), str(job_id), int(attempt_no), _now()),
        )


def _finish_attempt(server, call_uuid: str, outcome: str, detail: str = "") -> None:
    with server.db.transaction() as con:
        con.execute(
            "UPDATE redial_attempts SET ended_at=?,outcome=?,detail=? WHERE call_uuid=?",
            (_now(), str(outcome)[:80], str(detail)[:1000], str(call_uuid)),
        )


def _create_job(server, *, root_uuid: str, workspace_id: str, number: str, caller_id: str,
                contact_name: str, randomize_caller_id: bool, interval_seconds: float,
                max_attempts: int, attempts_made: int, current_call_uuid: str, state: str,
                operator_user_id: str = "", operator_name: str = "") -> dict[str, Any]:
    _ensure_schema(server)
    existing = _job_by_root(server, root_uuid)
    if existing:
        return existing
    interval_seconds = max(2.0, min(300.0, float(interval_seconds or 5)))
    max_attempts = max(2, min(50, int(max_attempts or 10)))
    attempts_made = max(0, min(max_attempts, int(attempts_made or 0)))
    now = _now(); job_id = uuid.uuid4().hex
    with server.db.transaction() as con:
        con.execute(
            """
            INSERT INTO redial_jobs(
              job_id,root_uuid,workspace_id,number,caller_id,contact_name,randomize_caller_id,
              interval_seconds,max_attempts,attempts_made,queue_failures,state,next_attempt_at,
              current_call_uuid,last_reason,last_error,operator_user_id,operator_name,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (job_id,str(root_uuid),str(workspace_id),str(number),str(caller_id),str(contact_name),
             int(bool(randomize_caller_id)),interval_seconds,max_attempts,attempts_made,0,str(state),0.0,
             str(current_call_uuid),"","",str(operator_user_id),str(operator_name),now,now),
        )
    if current_call_uuid:
        _insert_attempt(server, job_id, current_call_uuid, max(1, attempts_made))
    return _job_by_root(server, root_uuid) or {}


def _update_job(server, job_id: str, **changes) -> dict[str, Any] | None:
    allowed = {"interval_seconds","max_attempts","attempts_made","queue_failures","state",
               "next_attempt_at","current_call_uuid","last_reason","last_error","caller_id",
               "contact_name","randomize_caller_id"}
    payload = {k:v for k,v in changes.items() if k in allowed}
    if not payload:
        return None
    payload["updated_at"] = _now()
    fields = ", ".join(f"{k}=?" for k in payload)
    args = list(payload.values()) + [str(job_id)]
    with server.db.transaction() as con:
        con.execute(f"UPDATE redial_jobs SET {fields} WHERE job_id=?", tuple(args))
        row = con.execute("SELECT * FROM redial_jobs WHERE job_id=?", (str(job_id),)).fetchone()
    return _rowdict(row)


def _schedule_retry(server, job: dict[str, Any], reason: str, *, immediate: bool = False, error: str = "") -> bool:
    if not job or job.get("state") in TERMINAL_STATES or job.get("state") in {"paused","error"}:
        return False
    attempts = int(job.get("attempts_made",0) or 0); maximum = int(job.get("max_attempts",10) or 10)
    if attempts >= maximum:
        _update_job(server,job["job_id"],state="exhausted",next_attempt_at=0.0,current_call_uuid="",
                    last_reason="attempt limit reached",last_error=error)
        server.call_history.log_activity("auto redial exhausted",f"{attempts}/{maximum} attempts used",
                                         uuid=job["root_uuid"],number=job["number"])
        return False
    delay = 0.0 if immediate else float(job.get("interval_seconds",5) or 5)
    _update_job(server,job["job_id"],state="waiting",next_attempt_at=_now()+max(0.0,delay),
                current_call_uuid="",last_reason=_normalize_reason(reason),last_error=str(error)[:1000])
    return True


def _complete_job(server, job: dict[str, Any], reason: str = "answered") -> None:
    if not job:return
    _update_job(server,job["job_id"],state="answered",next_attempt_at=0.0,current_call_uuid="",
                last_reason=reason,last_error="")
    server.call_history.log_activity("auto redial completed",
        f"Stopped after answer on attempt {int(job.get('attempts_made',0) or 0)}",
        uuid=job["root_uuid"],number=job["number"])


def _stop_job(server, job: dict[str, Any], reason: str = "stopped by operator") -> None:
    if not job:return
    _update_job(server,job["job_id"],state="stopped",next_attempt_at=0.0,last_reason=reason,last_error="")
    server.call_history.log_activity("auto redial stopped",reason,uuid=job["root_uuid"],number=job["number"])


def _pause_job(server, job: dict[str, Any]) -> None:
    if job:_update_job(server,job["job_id"],state="paused",next_attempt_at=0.0,last_reason="paused")


def _resume_job(server, job: dict[str, Any], *, immediate: bool = False) -> None:
    if not job or job.get("state")=="answered":return
    attempts=int(job.get("attempts_made",0) or 0); maximum=int(job.get("max_attempts",10) or 10)
    if attempts>=maximum:
        _update_job(server,job["job_id"],state="exhausted",next_attempt_at=0.0);return
    wait=0.0 if immediate else float(job.get("interval_seconds",5) or 5)
    _update_job(server,job["job_id"],state="waiting",next_attempt_at=_now()+wait,last_reason="resumed",last_error="")


def _recover_jobs(server) -> None:
    for job in _list_jobs(server,include_terminal=False):
        if job["state"] in {"paused","error"}:continue
        if job["state"] in {"armed","starting","dialing","screening"}:
            _update_job(server,job["job_id"],state="waiting",next_attempt_at=_now()+2.0,
                        current_call_uuid="",last_reason="recovered after service restart")
        elif job["state"]=="waiting" and float(job.get("next_attempt_at",0) or 0)<=0:
            _update_job(server,job["job_id"],next_attempt_at=_now()+2.0)


async def _dial_due_job(server, job: dict[str, Any]) -> None:
    current=str(job.get("current_call_uuid","") or "")
    if current and (server.bot.bridge.get_session(current) or server.bot.bridge.get_pending(current)):
        _update_job(server,job["job_id"],state="dialing",next_attempt_at=_now()+1.0);return
    attempts=int(job.get("attempts_made",0) or 0); maximum=int(job.get("max_attempts",10) or 10)
    if attempts>=maximum:
        _update_job(server,job["job_id"],state="exhausted",next_attempt_at=0.0);return
    with server.db.transaction() as con:
        row=con.execute("SELECT state,next_attempt_at FROM redial_jobs WHERE job_id=?",(job["job_id"],)).fetchone()
        if not row or row["state"]!="waiting" or float(row["next_attempt_at"])>_now()+0.05:return
        con.execute("UPDATE redial_jobs SET state='starting',next_attempt_at=0,updated_at=? WHERE job_id=?",(_now(),job["job_id"]))
    attempt=attempts+1
    try:
        n,c,name,uid=server._queue_web_outbound(
            job["number"],job["caller_id"],job["contact_name"],
            randomize_caller_id=bool(job.get("randomize_caller_id")),source="auto-redial",
            retry_of=job["root_uuid"],retry_index=max(0,attempt-1),
            workspace_ids=[job["workspace_id"]] if job.get("workspace_id") else None,
            operator_user_id=job.get("operator_user_id",""),operator_name=job.get("operator_name",""))
    except ValueError as exc:
        _update_job(server,job["job_id"],state="error",next_attempt_at=0.0,last_reason="policy blocked",
                    last_error=server._sanitize_detail(exc))
        server.call_history.log_activity("auto redial blocked",server._sanitize_detail(exc),
                                         uuid=job["root_uuid"],number=job["number"]);return
    except Exception as exc:
        failures=int(job.get("queue_failures",0) or 0)+1; detail=server._sanitize_detail(exc)
        if failures>=MAX_QUEUE_FAILURES:
            _update_job(server,job["job_id"],state="error",next_attempt_at=0.0,queue_failures=failures,
                        last_reason="PBX/Discord unavailable",last_error=detail)
            server.call_history.log_activity("auto redial paused by error",f"Queue failed {failures} times: {detail}",
                                             uuid=job["root_uuid"],number=job["number"]);return
        backoff=min(30.0,max(2.0,float(job.get("interval_seconds",5) or 5),failures*2.0))
        _update_job(server,job["job_id"],state="waiting",next_attempt_at=_now()+backoff,queue_failures=failures,
                    last_reason="PBX/Discord unavailable",last_error=detail)
        server.call_history.log_activity("auto redial waiting",f"Queue unavailable; retrying in {backoff:g}s: {detail}",
                                         uuid=job["root_uuid"],number=job["number"]);return
    _insert_attempt(server,job["job_id"],uid,attempt)
    _update_job(server,job["job_id"],state="dialing",attempts_made=attempt,queue_failures=0,
                current_call_uuid=uid,next_attempt_at=0.0,caller_id=c,contact_name=name,
                last_reason=f"attempt {attempt}",last_error="")
    server.call_history.log_activity("auto redial attempt",f"Attempt {attempt}/{maximum}",uuid=uid,number=n)
    try:await server._publish("redial.attempt",{"job_id":job["job_id"],"root_uuid":job["root_uuid"],
        "uuid":uid,"attempt":attempt,"max_attempts":maximum,"number":n,"workspace_id":job.get("workspace_id","")})
    except Exception:pass


async def _worker_loop(server) -> None:
    try:
        while True:
            try:
                now=_now()
                for job in _list_jobs(server,include_terminal=False):
                    if job["state"]=="waiting" and float(job["next_attempt_at"] or 0)<=now:
                        await _dial_due_job(server,job)
            except asyncio.CancelledError:raise
            except Exception:log.exception("Auto-redial worker iteration failed")
            await asyncio.sleep(0.75)
    except asyncio.CancelledError:raise


async def _maybe_schedule_redial(server, uid: str, reason: str, info: dict) -> bool:
    job=_job_by_call(server,uid)
    if not job:return False
    reason=_normalize_reason(reason)
    if reason not in RETRY_REASONS:return False
    _finish_attempt(server,uid,reason,server._sanitize_detail(info.get("detail","")))
    return _schedule_retry(server,job,reason)


def _cancel_legacy_redial(server, uid: str) -> None:
    # Legacy teardown calls this for voicemail too; do not let it kill persistent jobs.
    server._auto_redial.pop(str(uid),None);task=server._redial_tasks.pop(str(uid),None)
    if task and not task.done():task.cancel()


async def _call_auto_redial(server, request):
    uid=str(request.match_info["uuid"]);await server._call_access(request,uid,"dial")
    row=server.call_history.get_by_uuid(uid);session=server.bot.bridge.get_session(uid);pending=server.bot.bridge.get_pending(uid)
    existing=_job_by_call(server,uid)
    try:body=await request.json()
    except Exception:body={}
    action=str(body.get("action","") or "").strip().lower();enabled=bool(body.get("enabled",True))
    if action=="stop" or not enabled:
        if existing:_stop_job(server,existing)
        return web.json_response({"ok":True,"message":"Auto redial stopped."})
    if action=="pause":
        if not existing:return web.json_response({"ok":False,"error":"Redial job not found."},status=404)
        _pause_job(server,existing);fresh=_job_by_root(server,existing["root_uuid"]) or existing
        return web.json_response({"ok":True,"message":"Auto redial paused.","job":_public_job(fresh)})
    if action in {"resume","retry_now"}:
        if not existing:return web.json_response({"ok":False,"error":"Redial job not found."},status=404)
        _resume_job(server,existing,immediate=action=="retry_now");fresh=_job_by_root(server,existing["root_uuid"]) or existing
        return web.json_response({"ok":True,"message":"Retry queued now." if action=="retry_now" else "Auto redial resumed.","job":_public_job(fresh)})
    if not row and not session and not pending:return web.json_response({"ok":False,"error":"Call not found."},status=404)
    interval=max(2.0,min(300.0,float(body.get("interval_seconds",body.get("delay",5)) or 5)))
    maximum=max(2,min(50,int(body.get("max_attempts",body.get("max_retries",10)) or 10)))
    if existing:
        fresh=_update_job(server,existing["job_id"],interval_seconds=interval,max_attempts=maximum,
                          randomize_caller_id=int(bool(body.get("randomize_caller_id",existing.get("randomize_caller_id"))))) or existing
        return web.json_response({"ok":True,"message":"Auto redial settings updated.","job":_public_job(fresh)})
    row=row or {};number=str((pending or {}).get("number") or getattr(session,"remote_number","") or row.get("number") or "")
    if not number:return web.json_response({"ok":False,"error":"Destination number unavailable."},status=400)
    if session:return web.json_response({"ok":False,"error":"This call is already answered. Start Auto Redial from the number field or from a failed/ringing call."},status=409)
    wsids=list((pending or {}).get("workspace_ids",[]) or row.get("workspace_ids",[]) or []);workspace_id=str(wsids[0]) if wsids else ""
    job=_create_job(server,root_uuid=uid,workspace_id=workspace_id,number=number,
        caller_id=str((pending or {}).get("caller_id") or row.get("caller_id") or ""),
        contact_name=str((pending or {}).get("contact_name") or row.get("contact_name") or ""),
        randomize_caller_id=bool(body.get("randomize_caller_id",False)),interval_seconds=interval,
        max_attempts=maximum,attempts_made=1 if pending else 0,current_call_uuid=uid if pending else "",
        state="dialing" if pending else "waiting",operator_user_id=str(row.get("operator_user_id","")),operator_name=str(row.get("operator_name","")))
    if not pending:
        _schedule_retry(server,job,_normalize_reason(row.get("outcome") or row.get("state") or "failed"),immediate=True)
        job=_job_by_root(server,uid) or job
    return web.json_response({"ok":True,"message":f"Auto redial job created: up to {maximum} attempts, {interval:g}s between attempts.","job":_public_job(job)})


async def _process_pending_timeouts(server, rows: list[dict]) -> None:
    for item in rows:
        uid=str(item.get("uuid","") or "")
        if not uid:continue
        detail=server._sanitize_detail(item.get("detail",""));server.call_history.fail(uid,outcome="no answer",diagnostic=detail)
        await server._maybe_schedule_redial(uid,"no answer",item)


REDIAL_UI = r'''<style id="pbx-redial-jobs-v2">
#redialJobsCard{margin:12px 0}.redialJob{display:grid;grid-template-columns:minmax(180px,1.4fr) minmax(170px,1fr) auto;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid #202c3d}.redialJob:last-child{border-bottom:0}.redialJob .rjState{font-weight:800}.redialJob .rjMeta{color:var(--muted);font-size:11px}.redialJob .rjActions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}#startAutoRedial{min-height:32px}[data-redial],[data-panel="redial"]{display:none!important}@media(max-width:760px){.redialJob{grid-template-columns:1fr}.redialJob .rjActions{justify-content:flex-start}}
</style><script id="pbx-redial-jobs-v2-script">(()=>{if(window.__pbxRedialJobsV2)return;window.__pbxRedialJobsV2=true;function st(){try{return typeof status!=="undefined"&&status&&typeof status==="object"?status:{}}catch(_){return{}}}function pn(v){try{return typeof phone==="function"?phone(v):String(v||"")}catch(_){return String(v||"")}}function left(j){const n=Math.max(0,Math.ceil((Number(j.next_attempt_at)||0)-Date.now()/1000));return n>0?`${n}s`:"now"}function state(j){const s=String(j.state||"");if(s==="waiting")return`Waiting ${left(j)}`;if(s==="starting")return"Starting next attempt";if(s==="dialing")return`Dialing attempt ${j.attempts_made}/${j.max_attempts}`;if(s==="screening")return`Checking answer · ${j.attempts_made}/${j.max_attempts}`;if(s==="paused")return"Paused";if(s==="error")return"Needs attention";return s||"Active"}function shell(){const calls=document.querySelector("#calls");if(!calls)return;if(!document.querySelector("#startAutoRedial")){const q=document.querySelector(".quicksettings");if(q){const b=document.createElement("button");b.id="startAutoRedial";b.className="btn warn";b.type="button";b.textContent="⟳ Auto Redial";b.title="Keep calling until a human answers or the attempt limit is reached";q.insertBefore(b,q.firstChild);b.onclick=openStart}}if(!document.querySelector("#redialJobsCard")){const out=document.querySelector("#pendingCalls")?.closest(".g2.grid"),c=document.createElement("section");c.className="card";c.id="redialJobsCard";c.innerHTML='<div class="sectionhead"><div><h2>Auto Redial</h2><div class="muted">Persistent retry jobs. They survive page refreshes and service restarts.</div></div><span class="pill" id="redialJobCount">0 running</span></div><div id="redialJobsList"><div class="empty">No auto-redial jobs.</div></div>';out?out.parentNode.insertBefore(c,out):calls.appendChild(c)}}function openStart(){const n=document.querySelector("#dialNumber")?.value?.trim();if(!n){toast("Enter a phone number first.",true);return}modal("Start Auto Redial",`<div class="field"><label>Destination</label><input id="rjNumber" value="${esc(n)}" inputmode="tel"></div><div class="g2 grid"><div class="field"><label>Seconds between attempts</label><input id="rjInterval" type="number" min="2" max="300" value="5"></div><div class="field"><label>Maximum attempts</label><input id="rjMax" type="number" min="2" max="50" value="10"></div></div><p class="muted small">Stops automatically when a human answer is confirmed. PBX/Discord queue outages do not consume a call attempt.</p><button class="btn primary" id="rjStartGo">Start calling</button>`);document.querySelector("#rjStartGo").onclick=async()=>{try{const j=await api("/api/dial",{method:"POST",body:{number:document.querySelector("#rjNumber").value,caller_id:document.querySelector("#callerId")?.value||"",randomize_caller_id:!!document.querySelector("#randomCid")?.checked,source:"auto-redial",auto_redial:{interval_seconds:+document.querySelector("#rjInterval").value||5,max_attempts:+document.querySelector("#rjMax").value||10}}});closeModal();toast(j.message||"Auto redial started");await refreshStatus()}catch(e){toast(e.message,true)}}}async function ctl(root,action){try{const j=await api(`/api/call/${encodeURIComponent(root)}/auto-redial`,{method:"POST",body:{action}});toast(j.message||"Updated");await refreshStatus()}catch(e){toast(e.message,true)}}function render(){shell();const box=document.querySelector("#redialJobsList"),count=document.querySelector("#redialJobCount");if(!box)return;const jobs=st().redial_jobs||[];if(count)count.textContent=`${jobs.length} running`;if(!jobs.length){box.innerHTML='<div class="empty">No auto-redial jobs.</div>';return}box.innerHTML=jobs.map(j=>`<div class="redialJob" data-rj="${esc(j.root_uuid)}"><div><b>${esc(j.contact_name||pn(j.number)||j.root_uuid.slice(0,8))}</b><div class="rjMeta">${esc(pn(j.number))}${j.caller_id?` · CID ${esc(pn(j.caller_id))}`:""}</div></div><div><div class="rjState">${esc(state(j))}</div><div class="rjMeta">${esc(j.last_reason||"")}${j.last_error?` · ${esc(j.last_error)}`:""}</div></div><div class="rjActions">${j.state==="paused"?'<button class="btn good" data-rj-action="resume">Resume</button>':'<button class="btn" data-rj-action="pause">Pause</button>'}<button class="btn" data-rj-action="retry_now">Retry now</button><button class="btn danger" data-rj-action="stop">Stop</button></div></div>`).join("");box.querySelectorAll("[data-rj-action]").forEach(b=>b.onclick=()=>ctl(b.closest("[data-rj]").dataset.rj,b.dataset.rjAction))}shell();const old=window.renderStatus;if(typeof old==="function")window.renderStatus=function(){const x=old.apply(this,arguments);render();return x};setInterval(()=>{if((st().redial_jobs||[]).some(j=>j.state==="waiting"))render()},1000);new MutationObserver(shell).observe(document.body,{childList:true,subtree:true})})();</script>'''


def _inject_ui(page: str) -> str:
    if REDIAL_UI_MARKER in page:return page
    return page.replace("</body>",REDIAL_UI+"\n</body>",1)


def apply() -> None:
    try:import webui_v3
    except ModuleNotFoundError:import webui as webui_v3
    cls=webui_v3.WebControlServer
    if getattr(cls,"_auto_redial_jobs_v2_applied",False):return
    original_init=cls.__init__;original_start=cls.start;original_close=cls.close;original_status=cls.status
    original_bridge=cls._bridge_event;original_dial=cls.dial;original_cancel=cls.cancel_outbound
    original_hangup=cls.call_hangup;original_hangup_all=cls.hangup_all
    def __init__(self,*args,**kwargs):
        original_init(self,*args,**kwargs);_ensure_schema(self);self._redial_job_worker=None
    async def start(self):
        _recover_jobs(self);await original_start(self)
        if not self._redial_job_worker or self._redial_job_worker.done():self._redial_job_worker=asyncio.create_task(_worker_loop(self),name="persistent-auto-redial")
    async def close(self):
        task=getattr(self,"_redial_job_worker",None)
        if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        self._redial_job_worker=None;await original_close(self)
    async def status_v2(self,request):
        timed=list(self.bot.bridge.drain_pending_timeouts())
        if timed:await _process_pending_timeouts(self,timed)
        response=await original_status(self,request)
        try:
            data=json.loads(response.text);actor=request["actor"];accessible=await self._actor_workspaces(actor)
            allowed={str(x["id"]) for x in accessible};selected=str(data.get("selected_workspace_id","") or "");scope={selected} if selected else allowed
            jobs=_list_jobs(self,scope,include_terminal=False);data["redial_jobs"]=[_public_job(x) for x in jobs]
            bycall={}
            for job in jobs:
                pub=_public_job(job)
                for cid in (job.get("root_uuid"),job.get("current_call_uuid")):
                    if cid:bycall[str(cid)]=pub
            for collection in ("calls","outbound_pending"):
                for item in data.get(collection,[]) or []:
                    if str(item.get("uuid","")) in bycall:item["redial_job"]=bycall[str(item.get("uuid",""))]
            return web.json_response(data,status=response.status)
        except Exception:log.exception("Could not decorate status with redial jobs");return response
    async def bridge_event(self,event,payload):
        await original_bridge(self,event,payload);uid=str(payload.get("uuid","") or "")
        if not uid:return
        job=_job_by_call(self,uid)
        if not job:return
        if event=="connected":
            _finish_attempt(self,uid,"connected")
            if bool(payload.get("voicemail_detection_enabled",False)):_update_job(self,job["job_id"],state="screening",current_call_uuid=uid,last_reason="checking answer")
            else:_complete_job(self,job,"answered")
        elif event=="voicemail_result":
            result=str(payload.get("result","") or "").upper()
            if result=="HUMAN":_finish_attempt(self,uid,"answered",str(payload.get("cause","") or ""));_complete_job(self,job,"human answered")
            elif result=="MACHINE":_finish_attempt(self,uid,"voicemail",str(payload.get("cause","") or ""));_schedule_retry(self,job,"voicemail")
            else:_finish_attempt(self,uid,"answered","voicemail detection uncertain");_complete_job(self,job,"answer uncertain; stopped safely")
        elif event=="ended":
            manual=bool(payload.get("manual",False));voicemail=bool(payload.get("voicemail",False));fresh=_job_by_call(self,uid) or job
            if manual:_finish_attempt(self,uid,"stopped");_stop_job(self,fresh,"stopped by operator")
            elif voicemail:_finish_attempt(self,uid,"voicemail");_schedule_retry(self,fresh,"voicemail")
            elif fresh.get("state") not in TERMINAL_STATES and fresh.get("state") not in {"waiting","error","paused"}:
                _finish_attempt(self,uid,"disconnected");_schedule_retry(self,fresh,"disconnected")
    async def dial_v2(self,request):
        try:body=await request.json()
        except Exception:body={}
        cfg=body.get("auto_redial");response=await original_dial(self,request)
        if not cfg or response.status>=300:return response
        try:
            data=json.loads(response.text);uid=str(data.get("uuid","") or "")
            if not uid:return response
            row=self.call_history.get_by_uuid(uid) or {};wsids=list(row.get("workspace_ids",[]) or [])
            job=_create_job(self,root_uuid=uid,workspace_id=str(wsids[0]) if wsids else "",
                number=str(data.get("number") or row.get("number") or ""),caller_id=str(data.get("caller_id") or row.get("caller_id") or ""),
                contact_name=str(data.get("contact_name") or row.get("contact_name") or ""),randomize_caller_id=bool(body.get("randomize_caller_id",False)),
                interval_seconds=float((cfg or {}).get("interval_seconds",5) or 5),max_attempts=int((cfg or {}).get("max_attempts",10) or 10),
                attempts_made=1,current_call_uuid=uid,state="dialing",operator_user_id=str(row.get("operator_user_id","")),operator_name=str(row.get("operator_name","")))
            data["redial_job"]=_public_job(job);data["message"]=f"Auto redial started: attempt 1/{job['max_attempts']}.";return web.json_response(data,status=response.status)
        except Exception:log.exception("Call queued but auto-redial job creation failed");return response
    async def cancel_v2(self,request):
        job=_job_by_call(self,str(request.match_info["uuid"]));
        if job:_stop_job(self,job,"outgoing attempt cancelled by operator")
        return await original_cancel(self,request)
    async def hangup_v2(self,request):
        job=_job_by_call(self,str(request.match_info["uuid"]));
        if job:_stop_job(self,job,"call hung up by operator")
        return await original_hangup(self,request)
    async def hangup_all_v2(self,request):
        try:
            _,ws=await self._workspace(request,"bridge")
            for job in _list_jobs(self,{str(ws["id"])},include_terminal=False):_stop_job(self,job,"workspace hangup all")
        except Exception:pass
        return await original_hangup_all(self,request)
    cls.__init__=__init__;cls.start=start;cls.close=close;cls.status=status_v2;cls._bridge_event=bridge_event;cls.dial=dial_v2
    cls.cancel_outbound=cancel_v2;cls.call_hangup=hangup_v2;cls.hangup_all=hangup_all_v2
    cls._cancel_auto_redial=_cancel_legacy_redial;cls._maybe_schedule_redial=_maybe_schedule_redial;cls.call_auto_redial=_call_auto_redial
    webui_v3.PAGE=_inject_ui(webui_v3.PAGE);cls._auto_redial_jobs_v2_applied=True
