from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web

log = logging.getLogger("discord-pbx.auto-redial")

NO_ANSWER_REASONS = {"no answer", "timeout", "failed", "busy"}
TERMINAL_REASONS = NO_ANSWER_REASONS | {"disconnected"}


def normalize_reason(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if text in {"noanswer", "not answered", "ring timeout", "ringing timeout"}:
        return "no answer"
    if "busy" in text:
        return "busy"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if text in {"disconnect", "disconnected", "dropped"}:
        return "disconnected"
    if text in {"fail", "failed", "error"}:
        return "failed"
    return text


def retry_allowed(policy: dict[str, Any], reason: str) -> bool:
    reason = normalize_reason(reason)
    mode = str(policy.get("retry_on", "all") or "all")
    if mode == "disconnect":
        return reason == "disconnected"
    if mode == "no-answer":
        return reason in NO_ANSWER_REASONS
    return reason in TERMINAL_REASONS


def terminal_reason(row: dict[str, Any] | None) -> str:
    row = row or {}
    for value in (row.get("outcome"), row.get("state")):
        reason = normalize_reason(value)
        if reason in TERMINAL_REASONS:
            return reason
    return ""


def _policy_key(server, uid: str) -> str:
    if uid in server._auto_redial:
        return uid
    for key, policy in server._auto_redial.items():
        if str(policy.get("root_uuid", "")) == uid:
            return key
    return uid


def cancel_auto_redial(server, uid: str) -> None:
    """Cancel a current retry or any child retry belonging to the requested root."""
    uid = str(uid)
    keys = [
        key for key, policy in list(server._auto_redial.items())
        if key == uid or str(policy.get("root_uuid", "")) == uid
    ]
    if not keys:
        keys = [uid]
    for key in keys:
        server._auto_redial.pop(key, None)
        task = server._redial_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()


async def maybe_schedule_redial(server, uid: str, reason: str, info: dict) -> bool:
    uid = str(uid)
    reason = normalize_reason(reason)
    policy = server._auto_redial.get(uid)
    if not policy or not policy.get("enabled") or not retry_allowed(policy, reason):
        return False

    existing_task = server._redial_tasks.get(uid)
    if existing_task and not existing_task.done():
        return True

    retries = max(0, int(policy.get("retries", 0) or 0))
    max_retries = max(1, min(20, int(policy.get("max_retries", 3) or 3)))
    if retries >= max_retries:
        policy["enabled"] = False
        policy["last_reason"] = "retry limit reached"
        server.call_history.log_activity(
            "auto redial stopped", f"Retry limit {max_retries} reached",
            uuid=uid, number=str(info.get("number", "")),
        )
        return False

    delay = max(1.0, min(300.0, float(policy.get("delay", 3) or 3)))
    row = server.call_history.get_by_uuid(uid) or {}
    number = str(info.get("number") or row.get("number") or "")
    caller_id = str(info.get("caller_id") or row.get("caller_id") or "")
    contact_name = str(info.get("contact_name") or row.get("contact_name") or "")
    workspace_ids = [str(x) for x in (info.get("workspace_ids") or row.get("workspace_ids") or []) if str(x)]
    operator_user_id = str(info.get("operator_user_id") or row.get("operator_user_id") or "")
    operator_name = str(info.get("operator_name") or row.get("operator_name") or "")
    if not number:
        policy["enabled"] = False
        policy["last_reason"] = "destination number unavailable"
        return False

    policy.setdefault("root_uuid", str(row.get("retry_of") or uid))
    if not policy.get("root_uuid"):
        policy["root_uuid"] = uid
    policy["last_reason"] = reason
    policy["next_retry_at"] = time.time() + delay
    policy["retries"] = retries

    async def worker() -> None:
        attempt = retries
        try:
            while True:
                current = server._auto_redial.get(uid)
                if not current or not current.get("enabled"):
                    return
                if attempt >= max_retries:
                    current["enabled"] = False
                    current["next_retry_at"] = 0
                    current["last_reason"] = "retry limit reached"
                    server.call_history.log_activity(
                        "auto redial stopped", f"Retry limit {max_retries} reached",
                        uuid=uid, number=number,
                    )
                    return

                current["next_retry_at"] = time.time() + delay
                await asyncio.sleep(delay)
                current = server._auto_redial.get(uid)
                if not current or not current.get("enabled"):
                    return

                attempt += 1
                current["retries"] = attempt
                current["next_retry_at"] = 0
                try:
                    n, actual_cid, cname, new_uid = server._queue_web_outbound(
                        number,
                        caller_id,
                        contact_name,
                        randomize_caller_id=bool(current.get("randomize_caller_id", False)),
                        source="redial",
                        retry_of=uid,
                        retry_index=attempt,
                        workspace_ids=workspace_ids or None,
                        operator_user_id=operator_user_id,
                        operator_name=operator_name,
                    )
                except ValueError as exc:
                    # Policy/DNC/outbound-disabled failures are not transient. Do not
                    # hammer a number that the current PBX policy says not to dial.
                    current["enabled"] = False
                    current["last_reason"] = server._sanitize_detail(exc)
                    server.call_history.log_activity(
                        "auto redial stopped", server._sanitize_detail(exc), uuid=uid, number=number,
                    )
                    return
                except Exception as exc:
                    # Voice connection, AMI availability and call-limit races can be
                    # transient. Consume one retry, retain the policy and try again.
                    current["last_reason"] = f"retry queue failed: {server._sanitize_detail(exc)}"
                    server.call_history.log_activity(
                        "auto redial retry failed",
                        f"Retry {attempt}/{max_retries}: {server._sanitize_detail(exc)}",
                        uuid=uid,
                        number=number,
                    )
                    if attempt >= max_retries:
                        current["enabled"] = False
                        current["next_retry_at"] = 0
                        return
                    continue

                next_policy = dict(current)
                next_policy.update({
                    "enabled": True,
                    "retries": attempt,
                    "next_retry_at": 0,
                    "last_reason": reason,
                    "root_uuid": str(current.get("root_uuid") or uid),
                })
                server._auto_redial.pop(uid, None)
                server._auto_redial[new_uid] = next_policy
                server.call_history.log_activity(
                    "auto redial",
                    f"Retry {attempt}/{max_retries} after {reason}",
                    uuid=new_uid,
                    number=n,
                )
                try:
                    await server._publish("call.auto_redial", {
                        "uuid": new_uid,
                        "retry_of": uid,
                        "root_uuid": next_policy["root_uuid"],
                        "retry_index": attempt,
                        "max_retries": max_retries,
                        "number": n,
                        "caller_id": actual_cid,
                        "contact_name": cname,
                        "workspace_ids": workspace_ids,
                    })
                except Exception:
                    pass
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = server._auto_redial.get(uid)
            if current:
                current["last_reason"] = server._sanitize_detail(exc)
                current["next_retry_at"] = 0
            server.call_history.log_activity(
                "auto redial failed", server._sanitize_detail(exc), uuid=uid, number=number,
            )
            log.exception("Auto redial worker failed for %s", uid)
        finally:
            server._redial_tasks.pop(uid, None)

    task = asyncio.create_task(worker(), name=f"auto-redial-{uid[:8]}")
    server._redial_tasks[uid] = task
    return True


async def process_pending_timeouts(server, timed_out_rows: list[dict]) -> int:
    scheduled = 0
    for timed_out in timed_out_rows:
        uid = str(timed_out.get("uuid", ""))
        if not uid:
            continue
        server.call_history.fail(
            uid,
            outcome="no answer",
            diagnostic=server._sanitize_detail(timed_out.get("detail", "")),
        )
        if await server._maybe_schedule_redial(uid, "no answer", timed_out):
            scheduled += 1
    return scheduled


async def call_auto_redial(server, request):
    uid = str(request.match_info["uuid"])
    await server._call_access(request, uid, "dial")
    session = server.bot.bridge.get_session(uid)
    pending = server.bot.bridge.get_pending(uid)
    row = server.call_history.get_by_uuid(uid)
    if not session and not pending and not row:
        return web.json_response({"ok": False, "error": "Call not found."}, status=404)

    try:
        body = await request.json()
        enabled = bool(body.get("enabled", True))
        key = _policy_key(server, uid)
        if not enabled:
            server._cancel_auto_redial(uid)
            server.call_history.log_activity(
                "auto redial disabled", "", uuid=uid, number=str((row or {}).get("number", "")),
            )
            return web.json_response({"ok": True, "message": "Auto redial disabled."})

        delay = max(1.0, min(300.0, float(body.get("delay", 3) or 3)))
        max_retries = max(1, min(20, int(body.get("max_retries", 3) or 3)))
        current = server._auto_redial.get(key, {})
        retry_on = str(body.get("retry_on", current.get("retry_on", "all")))
        if retry_on not in {"all", "disconnect", "no-answer"}:
            retry_on = "all"
        root_uuid = str(current.get("root_uuid") or uid)
        server._auto_redial[key] = {
            "enabled": True,
            "delay": delay,
            "max_retries": max_retries,
            "retries": int(current.get("retries", 0) or 0),
            "randomize_caller_id": bool(body.get("randomize_caller_id", current.get("randomize_caller_id", False))),
            "retry_on": retry_on,
            "next_retry_at": float(current.get("next_retry_at", 0) or 0),
            "last_reason": str(current.get("last_reason", "")),
            "root_uuid": root_uuid,
        }
        server.call_history.log_activity(
            "auto redial enabled",
            f"delay={delay:g}s max={max_retries} mode={retry_on}",
            uuid=uid,
            number=str((row or {}).get("number", "")),
        )

        # If the operator enables auto-redial after a call has already failed,
        # schedule it immediately. The old implementation merely armed a policy and
        # waited forever for a failure event that had already occurred.
        scheduled_now = False
        if not session and not pending and row:
            reason = terminal_reason(row)
            if reason and retry_allowed(server._auto_redial[key], reason):
                scheduled_now = await server._maybe_schedule_redial(key, reason, row)

        message = f"Auto redial enabled: up to {max_retries} retries, {delay:g}s delay."
        if scheduled_now:
            message += " Retry scheduled."
        return web.json_response({
            "ok": True,
            "message": message,
            "policy": server._auto_redial.get(key, {}),
            "scheduled": scheduled_now,
        })
    except (ValueError, TypeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)


def apply() -> None:
    try:
        import webui_v3
    except ModuleNotFoundError:
        import webui as webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_auto_redial_guard_applied", False):
        return

    original_status = cls.status

    async def status(self, request):
        # v3 used to drain pending ring timeouts before the legacy scheduler could
        # see them. Handle them first, then let the normal v3 status render.
        timed_out = list(self.bot.bridge.drain_pending_timeouts())
        if timed_out:
            await process_pending_timeouts(self, timed_out)
        return await original_status(self, request)

    cls._cancel_auto_redial = cancel_auto_redial
    cls._maybe_schedule_redial = maybe_schedule_redial
    cls.call_auto_redial = call_auto_redial
    cls.status = status
    cls._auto_redial_guard_applied = True
