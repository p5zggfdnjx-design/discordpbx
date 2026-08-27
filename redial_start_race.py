from __future__ import annotations

import json
import logging

from aiohttp import web

from auto_redial_guard import (
    RETRY_REASONS,
    TERMINAL_STATES,
    _complete_job,
    _finish_attempt,
    _job_by_root,
    _normalize_reason,
    _public_job,
    _schedule_retry,
    _update_job,
)

log = logging.getLogger("discord-pbx.auto-redial.start-race")


def reconcile_initial_job(server, root_uuid: str):
    """Reconcile the tiny window between first-call queueing and job persistence.

    The normal /api/dial path queues its asynchronous call starter before the
    persistent redial job is inserted. A very fast answer/failure can therefore
    emit its bridge event before the job exists. Once the job is inserted, this
    function observes the authoritative bridge/history state and catches the job
    up so it cannot remain stuck in `dialing` forever.
    """
    root_uuid = str(root_uuid or "")
    if not root_uuid:
        return None
    job = _job_by_root(server, root_uuid)
    if not job:
        return None
    if job.get("state") in TERMINAL_STATES or job.get("state") in {"waiting", "paused", "error"}:
        return job

    session = server.bot.bridge.get_session(root_uuid)
    pending = server.bot.bridge.get_pending(root_uuid)

    if session:
        if bool(getattr(session, "voicemail_detection_enabled", False)):
            return _update_job(
                server, job["job_id"], state="screening", current_call_uuid=root_uuid,
                next_attempt_at=0.0, last_reason="checking answer", last_error="",
            ) or job
        _finish_attempt(server, root_uuid, "answered")
        _complete_job(server, job, "answered before redial job initialization completed")
        return _job_by_root(server, root_uuid) or job

    if pending:
        return job

    row = server.call_history.get_by_uuid(root_uuid) or {}
    reason = _normalize_reason(row.get("outcome") or row.get("state") or "failed")
    if reason not in RETRY_REASONS:
        reason = "failed"
    _finish_attempt(server, root_uuid, reason, str(row.get("diagnostic", "") or ""))
    _schedule_retry(server, job, reason)
    return _job_by_root(server, root_uuid) or job


def apply() -> None:
    try:
        import webui_v3
    except ModuleNotFoundError:
        import webui as webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_auto_redial_start_race_guard_applied", False):
        return

    original_dial = cls.dial

    async def dial(self, request):
        response = await original_dial(self, request)
        if response.status >= 300:
            return response
        try:
            data = json.loads(response.text)
            descriptor = data.get("redial_job") if isinstance(data, dict) else None
            root_uuid = str((descriptor or {}).get("root_uuid", "") or "")
            if not root_uuid:
                return response
            fresh = reconcile_initial_job(self, root_uuid)
            if fresh:
                data["redial_job"] = _public_job(fresh)
                return web.json_response(data, status=response.status)
        except Exception:
            # The call itself has already been accepted. Never turn a successful
            # dial into an HTTP failure because post-queue reconciliation failed.
            log.exception("Could not reconcile initial auto-redial call state")
        return response

    cls.dial = dial
    cls._auto_redial_start_race_guard_applied = True
