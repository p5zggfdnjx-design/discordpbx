from __future__ import annotations

import asyncio
import logging
import time

from bridge import BridgeManager

log = logging.getLogger("discord-pbx.inbound-expiry")


def apply() -> None:
    if getattr(BridgeManager, "_inbound_expiry_guard", False):
        return

    old_init = BridgeManager.__init__
    old_prepare = BridgeManager.prepare_inbound
    old_cancel = BridgeManager.cancel_pending
    old_call_started = BridgeManager.call_started

    def __init__(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self._inbound_expiry_tasks: dict[str, asyncio.Task] = {}

    async def _expire_inbound_registration(self, call_uuid: str, deadline: float) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(max(0.0, deadline - time.time()))
            # inbound_voice_guard owns the actual pruning/history logic.
            prune = getattr(self, "_prune_inbound_pending", None)
            if callable(prune):
                prune()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Could not expire inbound registration %s", call_uuid)
        finally:
            if self._inbound_expiry_tasks.get(call_uuid) is current:
                self._inbound_expiry_tasks.pop(call_uuid, None)

    def _cancel_expiry(self, call_uuid: str) -> None:
        task = self._inbound_expiry_tasks.pop(str(call_uuid), None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def prepare_inbound(self, call_uuid: str, number: str = "", contact_name: str = "", workspace_ids=None) -> None:
        old_prepare(self, call_uuid, number, contact_name, workspace_ids)
        pending = self.get_pending(call_uuid) or {}
        deadline = float(pending.get("deadline_ts", 0) or 0)
        if not deadline:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _cancel_expiry(self, call_uuid)
        self._inbound_expiry_tasks[call_uuid] = loop.create_task(
            self._expire_inbound_registration(call_uuid, deadline),
            name=f"inbound-registration-expiry-{call_uuid[:8]}",
        )

    def cancel_pending(self, call_uuid: str) -> None:
        _cancel_expiry(self, call_uuid)
        old_cancel(self, call_uuid)

    async def call_started(self, session) -> bool:
        if getattr(session, "call_uuid", None):
            _cancel_expiry(self, session.call_uuid)
        return await old_call_started(self, session)

    BridgeManager.__init__ = __init__
    BridgeManager._expire_inbound_registration = _expire_inbound_registration
    BridgeManager._cancel_inbound_expiry = _cancel_expiry
    BridgeManager.prepare_inbound = prepare_inbound
    BridgeManager.cancel_pending = cancel_pending
    BridgeManager.call_started = call_started
    BridgeManager._inbound_expiry_guard = True
