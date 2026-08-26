from __future__ import annotations

import asyncio
import base64
import csv
import io
import re
import hmac
import html
import logging
import random
import time
import uuid
from pathlib import Path
from aiohttp import web

from audiosocket import DISCORD_FRAME_BYTES
from contacts import ContactsStore
from soundboard import SoundboardStore
from scheduled_calls import ScheduledCallStore
from operator_settings import OperatorSettingsStore
from caller_id_pool import CallerIdPoolStore
from random_call_pool import RandomCallPoolStore
from call_history import CallHistoryStore

log = logging.getLogger("discord-pbx.web")

MAX_BULK_PASTE_CHARS = 1_500_000
MAX_BULK_PHONE_ENTRIES = 25_000
BULK_RESPONSE_SAMPLE = 250

PAGE = (Path(__file__).with_name('web') / 'index.html').read_text(encoding='utf-8')

class WebControlServer:
    def __init__(self, bot, config):
        self.bot = bot
        self.config = config
        self.runner = None
        self.site = None
        self.contacts = ContactsStore(config.contacts_file)
        data_dir = Path(config.data_dir)
        self.soundboard = SoundboardStore(str(data_dir))
        self.scheduler = ScheduledCallStore(str(data_dir / "scheduled_calls.json"), max_pending=20)
        self.operator_settings = OperatorSettingsStore(str(data_dir / "operator_settings.json"))
        self.caller_id_pool = CallerIdPoolStore(
            str(data_dir / "caller_id_pool.yaml"),
            seed_numbers=config.ami_caller_id_options,
        )
        self.random_call_pool = RandomCallPoolStore(str(data_dir / "random_call_pool.yaml"))
        self.call_history = CallHistoryStore(str(data_dir / "call_history.sqlite3"))
        self.bot.bridge.event_callback = self._bridge_event
        self._auto_redial: dict[str, dict] = {}
        self._redial_tasks: dict[str, asyncio.Task] = {}
        self._last_random_call_number = ""
        self._last_random_caller_id = ""
        saved_operator = self.operator_settings.get()
        self.bot.bridge.set_ringback_muted(bool(saved_operator.get("ringback_muted", False)))
        self._voicemail_detection_enabled = bool(saved_operator.get("voicemail_detection_enabled", True))
        self.bot.bridge.voicemail_detection_enabled = self._voicemail_detection_enabled
        self.bot.bridge.set_master_gains(
            caller_to_discord=saved_operator.get("caller_to_discord_gain", 1.0),
            discord_to_caller=saved_operator.get("discord_to_caller_gain", 1.0),
            inbound_chime=saved_operator.get("inbound_chime_gain", 1.0),
        )
        self.scheduler_task = None
        self._outbound_tasks: dict[str, asyncio.Task] = {}
        # Atomic selection + queueing for rapid Random button presses.
        self._random_dial_lock = asyncio.Lock()

    @staticmethod
    def _validate_bulk_raw(raw: str) -> None:
        if len(raw) > MAX_BULK_PASTE_CHARS:
            raise ValueError(
                f"bulk paste is too large (max {MAX_BULK_PASTE_CHARS / 1_000_000:.1f} MB)"
            )

    @staticmethod
    def _bulk_payload(result: dict) -> dict:
        valid = list(result.get("valid", []))
        addable = list(result.get("addable", []))
        duplicates = list(result.get("duplicates", []))
        invalid = list(result.get("invalid", []))
        if len(valid) > MAX_BULK_PHONE_ENTRIES:
            raise ValueError(
                f"bulk paste contains too many phone numbers (max {MAX_BULK_PHONE_ENTRIES:,})"
            )
        return {
            "valid_count": len(valid),
            "addable_count": len(addable),
            "duplicate_count": len(duplicates),
            "invalid_count": len(invalid),
            "addable": addable[:BULK_RESPONSE_SAMPLE],
            "duplicates": duplicates[:BULK_RESPONSE_SAMPLE],
            "invalid": invalid[:BULK_RESPONSE_SAMPLE],
            "response_truncated": any(len(x) > BULK_RESPONSE_SAMPLE for x in (addable, duplicates, invalid)),
            "added": int(result.get("added", 0)),
        }

    def _ingress_authorized(self, request) -> bool:
        if request.path != "/api/pbx/inbound/register":
            return False
        supplied = request.query.get("token", "")
        return bool(self.config.pbx_ingress_token and supplied and hmac.compare_digest(supplied, self.config.pbx_ingress_token))

    def _authorized(self, request):
        if self._ingress_authorized(request):
            return True
        if self.config.web_auth_mode == "none":
            return True
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            username, password = base64.b64decode(header[6:]).decode().split(":", 1)
        except Exception:
            return False
        return hmac.compare_digest(username, self.config.web_username) and hmac.compare_digest(password, self.config.web_password)

    @web.middleware
    async def auth_middleware(self, request, handler):
        if self._authorized(request):
            return await handler(request)
        if request.path == "/api/pbx/inbound/register":
            return web.json_response({"ok": False, "error": "invalid PBX ingress token"}, status=403)
        return web.Response(status=401, text="Authentication required", headers={"WWW-Authenticate": 'Basic realm="Discord PBX"'})

    async def start(self):
        if self.config.web_auth_mode == "basic" and not self.config.web_password:
            raise RuntimeError("WEB_PASSWORD is required when WEB_AUTH_MODE=basic")
        app = web.Application(middlewares=[self.auth_middleware], client_max_size=12 * 1024 * 1024)
        app.add_routes([
            web.get("/", self.index), web.get("/api/status", self.status),
            web.get("/api/contacts", self.contacts_list), web.post("/api/contacts", self.contacts_create),
            web.put("/api/contacts/{contact_id}", self.contacts_update), web.delete("/api/contacts/{contact_id}", self.contacts_delete),
            web.get("/api/contacts.csv", self.contacts_csv), web.post("/api/contacts/import", self.contacts_import),
            web.post("/api/contacts/reorder", self.contacts_reorder),
            web.get("/api/history", self.history_list), web.put("/api/history/{row_id}", self.history_update),
            web.get("/api/activity", self.activity_list), web.get("/api/stats", self.stats),
            web.get("/api/diagnostics", self.diagnostics),
            web.get("/api/soundboard", self.soundboard_list), web.post("/api/soundboard/{slot}", self.soundboard_save),
            web.delete("/api/soundboard/{slot}", self.soundboard_delete), web.post("/api/soundboard/{slot}/play", self.soundboard_play),
            web.get("/api/schedules", self.schedules_list), web.post("/api/schedules", self.schedules_create),
            web.post("/api/schedules/{schedule_id}/cancel", self.schedules_cancel), web.delete("/api/schedules/{schedule_id}", self.schedules_delete),
            web.get("/api/pbx/inbound/register", self.inbound_register),
            web.post("/api/join", self.join), web.post("/api/leave", self.leave),
            web.post("/api/pbx/ping", self.pbx_ping), web.post("/api/dial", self.dial),
            web.post("/api/operator/ringback", self.ringback_setting),
            web.post("/api/operator/voicemail-detection", self.voicemail_detection_setting),
            web.get("/api/caller-id-pool", self.caller_id_pool_list),
            web.post("/api/caller-id-pool/preview", self.caller_id_pool_preview),
            web.post("/api/caller-id-pool/bulk", self.caller_id_pool_bulk),
            web.put("/api/caller-id-pool/{entry_id}", self.caller_id_pool_update),
            web.delete("/api/caller-id-pool/{entry_id}", self.caller_id_pool_delete),
            web.get("/api/caller-id-pool.yaml", self.caller_id_pool_yaml),
            web.get("/api/random-call-pool", self.random_call_pool_list),
            web.post("/api/random-call-pool/preview", self.random_call_pool_preview),
            web.post("/api/random-call-pool/bulk", self.random_call_pool_bulk),
            web.put("/api/random-call-pool/{entry_id}", self.random_call_pool_update),
            web.delete("/api/random-call-pool/{entry_id}", self.random_call_pool_delete),
            web.delete("/api/random-call-pool", self.random_call_pool_clear),
            web.get("/api/random-call-pool.yaml", self.random_call_pool_yaml),
            web.post("/api/dial-random", self.dial_random),
            web.post("/api/outbound/{uuid}/cancel", self.cancel_outbound),
            web.post("/api/call/{uuid}/routes", self.call_routes),
            web.post("/api/call/{uuid}/solo-talk", self.solo_talk),
            web.post("/api/call/{uuid}/focus", self.focus),
            web.post("/api/call/{uuid}/dtmf", self.call_dtmf),
            web.post("/api/call/{uuid}/auto-redial", self.call_auto_redial),
            web.put("/api/call/{uuid}/notes", self.call_notes),
            web.post("/api/call/{uuid}/split", self.call_split),
            web.post("/api/calls/conference", self.calls_conference),
            web.post("/api/call/{uuid}/hangup", self.call_hangup),
            web.post("/api/calls/talk", self.all_talk),
            web.post("/api/calls/listen", self.all_listen),
            web.post("/api/operator/audio", self.operator_audio),
            web.post("/api/calls/all-routes", self.all_routes),
            web.post("/api/calls/hangup-all", self.hangup_all),
        ])
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.config.web_bind, self.config.web_port)
        await self.site.start()
        self.scheduler_task = asyncio.create_task(self._scheduler_loop(), name="scheduled-call-worker")
        log.info("Web control panel listening on http://%s:%s", self.config.web_bind, self.config.web_port)

    async def close(self):
        for task in list(self._redial_tasks.values()):
            task.cancel()
        if self._redial_tasks:
            await asyncio.gather(*self._redial_tasks.values(), return_exceptions=True)
        self._redial_tasks.clear()
        for task in list(self._outbound_tasks.values()):
            task.cancel()
        if self._outbound_tasks:
            await asyncio.gather(*self._outbound_tasks.values(), return_exceptions=True)
        self._outbound_tasks.clear()
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
            self.scheduler_task = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = self.site = None

    async def index(self, request):
        return web.Response(
            text=PAGE,
            content_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    def _decorate_contact(self, item: dict) -> dict:
        item = dict(item)
        if item.get("number"):
            contact = self.contacts.find_by_number(str(item.get("number", "")))
            if contact:
                # Prefer the current contact name so renames immediately update active,
                # scheduled, and historical UI labels without rewriting call records.
                item["contact_name"] = contact.get("name", "")
        return item

    def _sanitize_detail(self, detail: object) -> str:
        text = str(detail or "")[:2000]
        for secret in (self.config.ami_secret, self.config.web_password, self.config.pbx_ingress_token):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)(secret|password|token)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", text)
        return text[:1000]

    @staticmethod
    def _failure_outcome(detail: object) -> str:
        """Map common PBX/carrier text to a concise operator outcome."""
        text = str(detail or "").lower()
        if "busy" in text or "user busy" in text:
            return "busy"
        if any(x in text for x in ("no answer", "noanswer", "not answered", "ring timeout")):
            return "no answer"
        if any(x in text for x in ("reject", "declin", "forbidden", "not permitted", "unauthorized")):
            return "rejected"
        if "timeout" in text or "timed out" in text:
            return "timeout"
        return "failed"

    async def _bridge_event(self, event: str, payload: dict) -> None:
        uid = str(payload.get("uuid", ""))
        if not uid:
            return
        if event == "connected":
            self.call_history.connected(
                uid,
                direction=str(payload.get("direction", "")), number=str(payload.get("number", "")),
                caller_id=str(payload.get("caller_id", "")), contact_name=str(payload.get("contact_name", "")),
                source=str(payload.get("source", "")),
            )
            if bool(payload.get("voicemail_detection_enabled", False)) and str(payload.get("direction", "")) == "outbound":
                self.call_history.set_state(uid, "checking voicemail")
                self.call_history.log_activity("voicemail check", "Analyzing the answered call before opening audio", uuid=uid, number=str(payload.get("number", "")))
            else:
                self.call_history.log_activity("call connected", payload.get("contact_name") or payload.get("number") or uid[:8], uuid=uid, number=str(payload.get("number", "")))
        elif event == "voicemail_result":
            result = str(payload.get("result", "NOTSURE") or "NOTSURE").upper()
            cause = str(payload.get("cause", "") or "")
            if result == "MACHINE":
                self.call_history.set_state(uid, "voicemail detected")
                self.call_history.log_activity("voicemail detected", cause or "machine-like greeting", uuid=uid, number=str(payload.get("number", "")))
                self._cancel_auto_redial(uid)
            elif result == "HUMAN":
                self.call_history.set_state(uid, "connected")
                self.call_history.log_activity("human detected", cause or "short greeting", uuid=uid, number=str(payload.get("number", "")))
            else:
                self.call_history.set_state(uid, "connected")
                self.call_history.log_activity("voicemail uncertain", "Detection was inconclusive; call kept open", uuid=uid, number=str(payload.get("number", "")))
        elif event == "ended":
            manual = bool(payload.get("manual", False))
            voicemail = bool(payload.get("voicemail", False))
            if voicemail:
                outcome = "voicemail"
                diagnostic = f"voicemail detected ({payload.get('voicemail_cause') or payload.get('voicemail_result') or 'machine'})"
                self.call_history.finish(uid, outcome=outcome, duration=float(payload.get("seconds", 0) or 0), diagnostic=diagnostic)
                self.call_history.log_activity("call ended", f"voicemail; {float(payload.get('seconds', 0) or 0):.1f}s", uuid=uid, number=str(payload.get("number", "")))
                self._cancel_auto_redial(uid)
            else:
                outcome = "completed" if manual else "disconnected"
                self.call_history.finish(uid, outcome=outcome, duration=float(payload.get("seconds", 0) or 0))
                self.call_history.log_activity("call ended", f"{outcome}; {float(payload.get('seconds', 0) or 0):.1f}s", uuid=uid, number=str(payload.get("number", "")))
                if not manual:
                    await self._maybe_schedule_redial(uid, "disconnected", payload)
                else:
                    self._cancel_auto_redial(uid)

    def _cancel_auto_redial(self, uid: str) -> None:
        self._auto_redial.pop(uid, None)
        task = self._redial_tasks.pop(uid, None)
        if task and not task.done():
            task.cancel()

    async def _maybe_schedule_redial(self, uid: str, reason: str, info: dict) -> bool:
        policy = self._auto_redial.get(uid)
        if not policy or not policy.get("enabled"):
            return False
        retry_on = str(policy.get("retry_on", "all") or "all")
        if retry_on == "disconnect" and reason != "disconnected":
            return False
        if retry_on == "no-answer" and reason not in {"no answer", "timeout", "failed", "busy"}:
            return False
        if uid in self._redial_tasks and not self._redial_tasks[uid].done():
            return True
        retries = int(policy.get("retries", 0) or 0)
        max_retries = max(1, min(20, int(policy.get("max_retries", 3) or 3)))
        if retries >= max_retries:
            policy["enabled"] = False
            policy["last_reason"] = "retry limit reached"
            self.call_history.log_activity("auto redial stopped", f"Retry limit {max_retries} reached", uuid=uid, number=str(info.get("number", "")))
            return False
        delay = max(1.0, min(300.0, float(policy.get("delay", 3) or 3)))
        row = self.call_history.get_by_uuid(uid) or {}
        number = str(info.get("number") or row.get("number") or "")
        caller_id = str(info.get("caller_id") or row.get("caller_id") or "")
        contact_name = str(info.get("contact_name") or row.get("contact_name") or "")
        if not number:
            policy["enabled"] = False
            return False
        policy["last_reason"] = reason
        policy["next_retry_at"] = time.time() + delay
        policy["retries"] = retries

        async def worker() -> None:
            try:
                await asyncio.sleep(delay)
                current = self._auto_redial.get(uid)
                if not current or not current.get("enabled"):
                    return
                new_retry = retries + 1
                n, actual_cid, cname, new_uid = self._queue_web_outbound(
                    number, caller_id, contact_name,
                    randomize_caller_id=bool(current.get("randomize_caller_id", False)),
                    source="redial", retry_of=uid, retry_index=new_retry,
                )
                next_policy = dict(current)
                next_policy.update({"retries": new_retry, "next_retry_at": 0, "last_reason": reason, "enabled": True})
                self._auto_redial.pop(uid, None)
                self._auto_redial[new_uid] = next_policy
                self.call_history.log_activity("auto redial", f"Retry {new_retry}/{max_retries} after {reason}", uuid=new_uid, number=n)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                current = self._auto_redial.get(uid)
                if current:
                    current["last_reason"] = self._sanitize_detail(exc)
                self.call_history.log_activity("auto redial failed", self._sanitize_detail(exc), uuid=uid, number=number)
                log.exception("Auto redial failed for %s", uid)
            finally:
                self._redial_tasks.pop(uid, None)

        task = asyncio.create_task(worker(), name=f"auto-redial-{uid[:8]}")
        self._redial_tasks[uid] = task
        return True

    async def status(self, request):
        # IMPORTANT: status is polled every ~1.2s by the web UI. Never parse or
        # serialize an entire caller/destination pool here. With 5,000 + 5,000
        # YAML entries the v1.6 implementation could spend multiple seconds per
        # status request, creating an ever-growing request backlog.
        data = self.bot.bridge.status_dict()
        for timed_out in self.bot.bridge.drain_pending_timeouts():
            uid = str(timed_out.get("uuid", ""))
            if uid:
                self.call_history.fail(uid, outcome="no answer", diagnostic=self._sanitize_detail(timed_out.get("detail", "")))
                await self._maybe_schedule_redial(uid, "no answer", timed_out)
        data["calls"] = [self._decorate_contact(x) for x in data.get("calls", [])]
        for item in data["calls"]:
            uid = str(item.get("uuid", ""))
            item["auto_redial"] = dict(self._auto_redial.get(uid, {}))
            row = self.call_history.get_by_uuid(uid) or {}
            item["notes"] = row.get("notes", "")
            item["disposition"] = row.get("disposition", "")
        for item in data.get("outbound_pending", []):
            item["auto_redial"] = dict(self._auto_redial.get(str(item.get("uuid", "")), {}))
        data["history"] = [self._decorate_contact(x) for x in data.get("history", [])]
        data["call"] = data["calls"][0] if len(data["calls"]) == 1 else None
        data["ami_configured"] = self.bot.ami.configured

        cid_total, cid_enabled = self.caller_id_pool.counts()
        random_total, random_enabled = self.random_call_pool.counts()
        busy_destinations = self._busy_destination_numbers()
        random_enabled_numbers = self.random_call_pool.enabled_number_set()
        random_available_count = len(random_enabled_numbers.difference(busy_destinations))
        used_slots = int(data.get("call_count", 0)) + int(data.get("outbound_pending_count", 0))

        data.update({
            # The browser only needs counts/availability here. The full pools
            # have dedicated endpoints and are never included in the hot poll.
            "default_caller_id": self.config.ami_caller_id,
            "allow_custom_caller_id": True,
            "random_caller_id_available": cid_enabled > 0,
            "caller_id_pool_count": cid_total,
            "caller_id_pool_enabled_count": cid_enabled,
            "random_call_pool_count": random_total,
            "random_call_pool_enabled_count": random_enabled,
            "random_call_pool_available_count": random_available_count,
            "random_call_available": random_available_count > 0 and used_slots < self.config.max_simultaneous_calls,
            "max_simultaneous_calls": self.config.max_simultaneous_calls,
            "voicemail_detection_enabled": bool(self._voicemail_detection_enabled),
            "voicemail_detection_supported": True,
            "scheduled_calls": [self._decorate_contact(x) for x in self.scheduler.list()],
            "stats": self.call_history.stats(),
        })
        return web.json_response(data)

    def _validate_contact(self, body: dict) -> dict:
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValueError("contact name is required")
        if len(name) > 80:
            raise ValueError("contact name is too long")
        number = self.bot.ami.normalize_number(str(body.get("number", "")))
        group = str(body.get("group", "")).strip()
        notes = str(body.get("notes", "")).strip()
        favorite = bool(body.get("favorite", False))
        if len(group) > 40:
            raise ValueError("contact group is too long")
        if len(notes) > 500:
            raise ValueError("contact notes are too long")
        return {"name": name, "number": number, "group": group, "notes": notes, "favorite": favorite}

    async def contacts_list(self, request):
        return web.json_response({"ok": True, "contacts": self.contacts.list()})

    async def contacts_create(self, request):
        try:
            values = self._validate_contact(await request.json())
            contact = self.contacts.create(**values)
            self.call_history.log_activity("contact added", contact.get("name", ""), number=contact.get("number", ""))
            return web.json_response({"ok": True, "contact": contact})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def contacts_update(self, request):
        try:
            values = self._validate_contact(await request.json())
            contact = self.contacts.update(request.match_info["contact_id"], **values)
            if not contact:
                return web.json_response({"ok": False, "error": "Contact not found."}, status=404)
            self.call_history.log_activity("contact updated", contact.get("name", ""), number=contact.get("number", ""))
            return web.json_response({"ok": True, "contact": contact})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def contacts_delete(self, request):
        contact = self.contacts.get(request.match_info["contact_id"])
        if not self.contacts.delete(request.match_info["contact_id"]):
            return web.json_response({"ok": False, "error": "Contact not found."}, status=404)
        if contact:
            self.call_history.log_activity("contact deleted", contact.get("name", ""), number=contact.get("number", ""))
        return web.json_response({"ok": True})

    async def contacts_reorder(self, request):
        try:
            body = await request.json()
            ids = [str(x) for x in body.get("contact_ids", [])][:5000]
            count = self.contacts.reorder(ids)
            self.call_history.log_activity("quick dial reordered", f"{count} positioned contacts")
            return web.json_response({"ok": True, "message": f"Quick Dial order saved for {count} contacts."})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def contacts_csv(self, request):
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["Name", "Number", "Group", "Notes", "Favorite"])
        writer.writeheader()
        for c in self.contacts.list():
            writer.writerow({
                "Name": c.get("name", ""), "Number": c.get("number", ""), "Group": c.get("group", ""),
                "Notes": c.get("notes", ""), "Favorite": "true" if c.get("favorite") else "false",
            })
        return web.Response(
            text=buf.getvalue(), content_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="discord-pbx-contacts.csv"'},
        )

    async def contacts_import(self, request):
        try:
            body = await request.json()
            raw = str(body.get("csv_text", ""))
            if not raw.strip():
                raise ValueError("CSV is empty")
            if len(raw) > 1_500_000:
                raise ValueError("CSV is too large (max 1.5 MB)")
            reader = csv.DictReader(io.StringIO(raw))
            if not reader.fieldnames:
                raise ValueError("CSV needs a header row")
            fields = {str(x).strip().lower(): x for x in reader.fieldnames}
            name_key = fields.get("name")
            number_key = fields.get("number") or fields.get("phone") or fields.get("telephone")
            if not name_key or not number_key:
                raise ValueError("CSV must contain Name and Number columns")
            created = updated = skipped = 0
            errors: list[str] = []
            for idx, row in enumerate(reader, start=2):
                if idx > 10002:
                    raise ValueError("CSV import is limited to 10,000 rows")
                try:
                    name = str(row.get(name_key, "") or "").strip()
                    number = self.bot.ami.normalize_number(str(row.get(number_key, "") or ""))
                    if not name or not number:
                        skipped += 1
                        continue
                    group = str(row.get(fields.get("group", ""), "") or "").strip()[:40]
                    notes = str(row.get(fields.get("notes", ""), "") or "").strip()[:500]
                    fav_raw = str(row.get(fields.get("favorite", ""), "") or "").strip().lower()
                    favorite = fav_raw in {"1", "true", "yes", "y", "favorite", "starred"}
                    existing = self.contacts.find_by_number(number)
                    if existing:
                        self.contacts.update(existing["id"], name=name, number=number, group=group, notes=notes, favorite=favorite or bool(existing.get("favorite")))
                        updated += 1
                    else:
                        self.contacts.create(name=name, number=number, group=group, notes=notes, favorite=favorite)
                        created += 1
                except Exception as exc:
                    skipped += 1
                    if len(errors) < 12:
                        errors.append(f"row {idx}: {exc}")
            self.call_history.log_activity("contacts imported", f"{created} created, {updated} updated, {skipped} skipped")
            return web.json_response({"ok": True, "created": created, "updated": updated, "skipped": skipped, "errors": errors, "message": f"Imported contacts: {created} new, {updated} updated, {skipped} skipped."})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def history_list(self, request):
        try:
            result = self.call_history.list_calls(
                limit=int(request.query.get("limit", "250")), offset=int(request.query.get("offset", "0")),
                q=str(request.query.get("q", ""))[:120], direction=str(request.query.get("direction", ""))[:20],
                outcome=str(request.query.get("outcome", ""))[:40],
                answered=str(request.query.get("answered", "0")).lower() in {"1", "true", "yes"},
                missed=str(request.query.get("missed", "0")).lower() in {"1", "true", "yes"},
            )
            result["calls"] = [self._decorate_contact(x) for x in result["calls"]]
            return web.json_response({"ok": True, **result})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def history_update(self, request):
        try:
            row_id = int(request.match_info["row_id"])
            row = self.call_history.get_by_id(row_id)
            if not row:
                return web.json_response({"ok": False, "error": "History entry not found."}, status=404)
            body = await request.json()
            notes = str(body.get("notes", row.get("notes", "")))[:2000] if "notes" in body else None
            disposition = str(body.get("disposition", row.get("disposition", "")))[:80] if "disposition" in body else None
            self.call_history.update_notes(str(row.get("uuid", "")), notes=notes, disposition=disposition)
            self.call_history.log_activity("history updated", disposition or "notes updated", uuid=str(row.get("uuid", "")), number=str(row.get("number", "")))
            return web.json_response({"ok": True, "call": self.call_history.get_by_id(row_id)})
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def activity_list(self, request):
        return web.json_response({"ok": True, "activity": self.call_history.activity(int(request.query.get("limit", "200")))})

    async def stats(self, request):
        return web.json_response({"ok": True, "stats": self.call_history.stats()})

    async def diagnostics(self, request):
        uid = str(request.query.get("uuid", ""))
        row = self.call_history.get_by_uuid(uid) if uid else None
        status = self.bot.bridge.status_dict()
        return web.json_response({
            "ok": True,
            "diagnostic": {
                "uuid": uid,
                "ami_configured": bool(self.bot.ami.configured),
                "discord_connected": bool(status.get("discord_connected")),
                "active_calls": int(status.get("call_count", 0)),
                "pending_calls": int(status.get("outbound_pending_count", 0)),
                "max_calls": int(self.config.max_simultaneous_calls),
                "call": row or {},
                "recent_activity": self.call_history.activity(12),
            },
        })

    def _normalize_web_caller_id(self, raw: str) -> str:
        raw = str(raw or "").strip() or self.config.ami_caller_id
        if not raw:
            return ""
        # v1.2 web console uses one plain global caller-ID number field.
        # Formatting characters are stripped, but names and AMI header syntax are not accepted.
        value = self.bot.ami.normalize_number(raw)
        if "*" in value or "#" in value:
            raise ValueError("caller ID must be a phone-number style value")
        return value

    def _authorized_caller_id_pool(self) -> list[str]:
        """Return enabled caller IDs from the persistent web-managed pool."""
        return self.caller_id_pool.enabled_numbers()

    def _busy_caller_ids(self) -> set[str]:
        busy: set[str] = set()
        for session in self.bot.bridge.get_sessions():
            value = str(getattr(session, "caller_id", "") or "").strip()
            if value:
                busy.add(value)
        for item in self.bot.bridge.outbound_pending():
            value = str(item.get("caller_id", "") or "").strip()
            if value:
                busy.add(value)
        return busy

    def _choose_web_caller_id(self, raw: str, randomize: bool = False) -> str:
        if not randomize:
            return self._normalize_web_caller_id(raw)
        pool = self._authorized_caller_id_pool()
        if not pool:
            raise ValueError("Random Caller ID needs at least one enabled number in the Caller ID Pool")

        # When calls are being launched rapidly, prefer a CID not already used by
        # another active/pending call and never repeat the immediately previous CID
        # when another option exists. If the pool is smaller than the call count we
        # gracefully fall back to reuse instead of blocking the call.
        busy = self._busy_caller_ids()
        fresh = [x for x in pool if x not in busy]
        choices = fresh or list(pool)
        if len(choices) > 1 and self._last_random_caller_id:
            alternatives = [x for x in choices if x != self._last_random_caller_id]
            if alternatives:
                choices = alternatives
        picked = random.choice(choices)
        self._last_random_caller_id = picked
        return picked

    async def caller_id_pool_list(self, request):
        # Pagination keeps very large pools from generating thousands of DOM rows
        # or multi-megabyte JSON responses on every management refresh.
        try:
            offset = max(0, int(request.query.get("offset", "0")))
            limit = max(1, min(250, int(request.query.get("limit", "100"))))
        except ValueError:
            offset, limit = 0, 100
        query = str(request.query.get("q", ""))[:120]
        items, filtered_total = self.caller_id_pool.page(offset=offset, limit=limit, query=query)
        total, enabled = self.caller_id_pool.counts()
        return web.json_response({
            "ok": True,
            "caller_ids": items,
            "offset": offset,
            "limit": limit,
            "filtered_total": filtered_total,
            "total_count": total,
            "enabled_count": enabled,
            "path": "/app/data/caller_id_pool.yaml",
        })

    async def caller_id_pool_preview(self, request):
        try:
            body = await request.json()
            raw = str(body.get("text", ""))
            self._validate_bulk_raw(raw)
            result = self._bulk_payload(await asyncio.to_thread(self.caller_id_pool.preview_bulk, raw))
            return web.json_response({"ok": True, **result})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def caller_id_pool_bulk(self, request):
        try:
            body = await request.json()
            raw = str(body.get("text", ""))
            action = str(body.get("action", "add")).strip().lower()
            self._validate_bulk_raw(raw)
            if action == "remove":
                result = await asyncio.to_thread(self.caller_id_pool.remove_bulk, raw)
                self.call_history.log_activity("caller ID pool bulk removed", f"{result['removed']} removed; {len(result['missing'])} not present")
                return web.json_response({
                    "ok": True, **result,
                    "message": f"Removed {result['removed']} caller ID(s); {len(result['missing'])} not present.",
                })
            if action != "add":
                raise ValueError("bulk action must be add or remove")
            raw_result = await asyncio.to_thread(self.caller_id_pool.preview_bulk, raw)
            checked = self._bulk_payload(raw_result)
            result = self._bulk_payload(await asyncio.to_thread(self.caller_id_pool.add_bulk, raw))
            self.call_history.log_activity("caller ID pool imported", f"{result['added']} added; {checked['duplicate_count']} duplicates")
            return web.json_response({
                "ok": True,
                **result,
                "message": f"Added {result['added']} caller ID(s); {checked['duplicate_count']} duplicate(s) skipped.",
            })
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def caller_id_pool_update(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get("enabled")) if "enabled" in body else None
        label = str(body.get("label", "")) if "label" in body else None
        item = self.caller_id_pool.update(request.match_info["entry_id"], enabled=enabled, label=label)
        if not item:
            return web.json_response({"ok": False, "error": "Caller ID not found."}, status=404)
        self.call_history.log_activity("caller ID updated", f"{item.get('number','')} enabled={bool(item.get('enabled'))}")
        return web.json_response({"ok": True, "caller_id": item})

    async def caller_id_pool_delete(self, request):
        entry_id = request.match_info["entry_id"]
        if not self.caller_id_pool.delete(entry_id):
            return web.json_response({"ok": False, "error": "Caller ID not found."}, status=404)
        self.call_history.log_activity("caller ID removed", entry_id)
        return web.json_response({"ok": True})

    async def caller_id_pool_yaml(self, request):
        return web.Response(
            text=self.caller_id_pool.yaml_text(),
            content_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="caller_id_pool.yaml"'},
        )

    async def random_call_pool_list(self, request):
        try:
            offset = max(0, int(request.query.get("offset", "0")))
            limit = max(1, min(250, int(request.query.get("limit", "100"))))
        except ValueError:
            offset, limit = 0, 100
        query = str(request.query.get("q", ""))[:120]
        items, filtered_total = self.random_call_pool.page(offset=offset, limit=limit, query=query)
        total, enabled = self.random_call_pool.counts()
        return web.json_response({
            "ok": True,
            "call_targets": items,
            "offset": offset,
            "limit": limit,
            "filtered_total": filtered_total,
            "total_count": total,
            "enabled_count": enabled,
            "path": "/app/data/random_call_pool.yaml",
        })
    
    async def random_call_pool_preview(self, request):
        try:
            body = await request.json()
            raw = str(body.get("text", ""))
            self._validate_bulk_raw(raw)
            result = self._bulk_payload(await asyncio.to_thread(self.random_call_pool.preview_bulk, raw))
            return web.json_response({"ok": True, **result})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
    
    async def random_call_pool_bulk(self, request):
        try:
            body = await request.json()
            raw = str(body.get("text", ""))
            action = str(body.get("action", "add")).strip().lower()
            self._validate_bulk_raw(raw)
            if action == "remove":
                result = await asyncio.to_thread(self.random_call_pool.remove_bulk, raw)
                self.call_history.log_activity("random pool bulk removed", f"{result['removed']} removed; {len(result['missing'])} not present")
                return web.json_response({
                    "ok": True, **result,
                    "message": f"Removed {result['removed']} random-call target(s); {len(result['missing'])} not present.",
                })
            if action != "add":
                raise ValueError("bulk action must be add or remove")
            raw_result = await asyncio.to_thread(self.random_call_pool.preview_bulk, raw)
            checked = self._bulk_payload(raw_result)
            result = self._bulk_payload(await asyncio.to_thread(self.random_call_pool.add_bulk, raw))
            self.call_history.log_activity("random pool imported", f"{result['added']} added; {checked['duplicate_count']} duplicates")
            return web.json_response({
                "ok": True,
                **result,
                "message": f"Added {result['added']} random-call target(s); {checked['duplicate_count']} duplicate(s) skipped.",
            })
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
    
    async def random_call_pool_update(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get("enabled")) if "enabled" in body else None
        label = str(body.get("label", "")) if "label" in body else None
        item = self.random_call_pool.update(request.match_info["entry_id"], enabled=enabled, label=label)
        if not item:
            return web.json_response({"ok": False, "error": "Random-call target not found."}, status=404)
        self.call_history.log_activity("random target updated", f"{item.get('number','')} enabled={bool(item.get('enabled'))}", number=str(item.get("number", "")))
        return web.json_response({"ok": True, "call_target": item})
    
    async def random_call_pool_delete(self, request):
        entry_id = request.match_info["entry_id"]
        if not self.random_call_pool.delete(entry_id):
            return web.json_response({"ok": False, "error": "Random-call target not found."}, status=404)
        self.call_history.log_activity("random target removed", entry_id)
        return web.json_response({"ok": True})
    
    async def random_call_pool_clear(self, request):
        removed = self.random_call_pool.clear()
        self.call_history.log_activity("random pool cleared", f"{removed} destinations removed")
        return web.json_response({
            "ok": True,
            "removed": removed,
            "message": f"Removed all {removed} random-call destination(s).",
        })
    
    async def random_call_pool_yaml(self, request):
        return web.Response(
            text=self.random_call_pool.yaml_text(),
            content_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="random_call_pool.yaml"'},
        )
    
    def _busy_destination_numbers(self) -> set[str]:
        busy: set[str] = set()
        for session in self.bot.bridge.get_sessions():
            value = str(getattr(session, "remote_number", "") or "").strip()
            if value:
                busy.add(value)
        for item in self.bot.bridge.outbound_pending():
            value = str(item.get("number", "") or "").strip()
            if value:
                busy.add(value)
        return busy

    def _available_random_targets(self) -> list[dict]:
        pool = self.random_call_pool.enabled()
        busy = self._busy_destination_numbers()
        return [x for x in pool if str(x.get("number", "")).strip() not in busy]

    def _choose_random_call_target(self) -> dict:
        pool = self.random_call_pool.enabled()
        if not pool:
            raise ValueError("Random Call needs at least one enabled number in the Random Call Pool")

        # A second tap should launch a *different* pool destination while the first
        # one is still starting/ringing/connected. Do not select a number already
        # active or pending. This also prevents accidental duplicate calls caused by
        # rapid taps on mobile.
        choices = self._available_random_targets()
        if not choices:
            raise ValueError("Every enabled Random Call destination is already active or pending")
        if len(choices) > 1 and self._last_random_call_number:
            alternatives = [x for x in choices if x.get("number") != self._last_random_call_number]
            if alternatives:
                choices = alternatives
        picked = random.choice(choices)
        self._last_random_call_number = str(picked.get("number", ""))
        return picked

    async def ringback_setting(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        muted = bool(body.get("muted", False))
        self.operator_settings.set_ringback_muted(muted)
        self.bot.bridge.set_ringback_muted(muted)
        self.call_history.log_activity("ringback setting", "muted" if muted else "enabled")
        return web.json_response({
            "ok": True,
            "ringback_muted": muted,
            "message": "Operator ringback muted." if muted else "Operator ringback enabled.",
        })

    async def voicemail_detection_setting(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get("enabled", False))
        self._voicemail_detection_enabled = enabled
        self.bot.bridge.voicemail_detection_enabled = enabled
        self.operator_settings.set_voicemail_detection(enabled)
        self.call_history.log_activity("voicemail detection", "enabled" if enabled else "disabled")
        return web.json_response({
            "ok": True,
            "voicemail_detection_enabled": enabled,
            "message": "Voicemail detection enabled." if enabled else "Voicemail detection disabled.",
        })

    async def schedules_list(self, request):
        return web.json_response({"ok": True, "scheduled_calls": self.scheduler.list()})

    async def schedules_create(self, request):
        try:
            body = await request.json()
            number = self.bot.ami.normalize_number(str(body.get("number", "")))
            randomize_caller_id = bool(body.get("randomize_caller_id", False))
            caller_id = self._normalize_web_caller_id(body.get("caller_id", ""))
            if randomize_caller_id and not self._authorized_caller_id_pool():
                raise ValueError("Random Caller ID needs at least one enabled number in the Caller ID Pool")
            recurrence = str(body.get("recurrence", "weekly"))
            timezone_name = str(body.get("timezone", "UTC"))
            local_time = str(body.get("local_time", ""))
            weekdays = body.get("weekdays", [])
            run_at = float(body.get("run_at", 0) or 0)
            contact = self.contacts.find_by_number(number)
            item = self.scheduler.create(
                number=number,
                caller_id=caller_id,
                recurrence=recurrence,
                timezone_name=timezone_name,
                local_time=local_time,
                weekdays=weekdays,
                run_at=run_at,
                contact_name=contact.get("name", "") if contact else "",
                randomize_caller_id=randomize_caller_id,
            )
            repeat_label = {"weekly": "Weekly", "daily": "Daily", "once": "One-time"}.get(item.get("recurrence"), "Scheduled")
            self.call_history.log_activity("schedule created", f"{repeat_label}: {number}", number=number)
            return web.json_response({"ok": True, "scheduled_call": item, "message": f"{repeat_label} call schedule saved."})
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def schedules_cancel(self, request):
        schedule_id = request.match_info["schedule_id"]
        if not self.scheduler.cancel(schedule_id):
            return web.json_response({"ok": False, "error": "Pending schedule not found."}, status=404)
        self.call_history.log_activity("schedule cancelled", schedule_id)
        return web.json_response({"ok": True, "message": "Scheduled call cancelled."})

    async def schedules_delete(self, request):
        schedule_id = request.match_info["schedule_id"]
        if not self.scheduler.delete(schedule_id):
            return web.json_response({"ok": False, "error": "Schedule not found."}, status=404)
        self.call_history.log_activity("schedule removed", schedule_id)
        return web.json_response({"ok": True})

    async def _scheduler_loop(self) -> None:
        try:
            while True:
                now = time.time()
                for item in self.scheduler.claim_due(now):
                    item_id = item["id"]
                    # Do not place a stale surprise call. Recurring rules advance to their
                    # next occurrence; a one-time rule is marked missed.
                    if now - float(item.get("run_at", now)) > 300:
                        self.scheduler.finish_occurrence(
                            item_id,
                            ok=False,
                            detail="Missed while bot was offline for more than 5 minutes",
                            missed=True,
                        )
                        self.call_history.log_activity("schedule missed", "Bot was offline more than 5 minutes", number=str(item.get("number", "")))
                        continue
                    try:
                        ok, detail, call_uuid = await self._dial_outbound(
                            item["number"],
                            item.get("caller_id", ""),
                            item.get("contact_name", ""),
                            randomize_caller_id=bool(item.get("randomize_caller_id", False)),
                            source="schedule",
                        )
                        self.scheduler.finish_occurrence(item_id, ok=ok, detail=detail, call_uuid=call_uuid)
                        self.call_history.log_activity("schedule fired", "queued" if ok else self._sanitize_detail(detail), uuid=call_uuid, number=str(item.get("number", "")))
                        log.info("Scheduled call %s fired: number=%s ok=%s uuid=%s", item_id, item["number"], ok, call_uuid)
                    except Exception as exc:
                        log.exception("Scheduled call %s failed", item_id)
                        self.scheduler.finish_occurrence(item_id, ok=False, detail=str(exc))
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise

    async def soundboard_list(self, request):
        return web.json_response({"ok": True, "slots": self.soundboard.list()})

    async def soundboard_save(self, request):
        try:
            slot = self.soundboard.validate_slot(int(request.match_info["slot"]))
            reader = await request.multipart()
            label = ""
            audio_data = None
            async for part in reader:
                if part.name == "label":
                    label = (await part.text()).strip()
                elif part.name == "file" and part.filename:
                    chunks = bytearray()
                    while True:
                        chunk = await part.read_chunk(size=65536)
                        if not chunk:
                            break
                        chunks.extend(chunk)
                        if len(chunks) > 10 * 1024 * 1024:
                            raise ValueError("sound file is larger than 10 MB")
                    if chunks:
                        audio_data = bytes(chunks)
            item = self.soundboard.save(slot, label=label, data=audio_data)
            self.call_history.log_activity("soundboard saved", f"slot {slot}: {item.get('label','')}")
            return web.json_response({"ok": True, "slot": item, "message": f"Sound {slot} saved."})
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def soundboard_delete(self, request):
        try:
            slot = self.soundboard.validate_slot(int(request.match_info["slot"]))
            self.soundboard.delete(slot)
            self.call_history.log_activity("soundboard cleared", f"slot {slot}")
            return web.json_response({"ok": True})
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def soundboard_play(self, request):
        try:
            slot = self.soundboard.validate_slot(int(request.match_info["slot"]))
            item = self.soundboard.get(slot)
            if not item["configured"]:
                raise ValueError("this soundboard slot has no audio file")
            try:
                body = await request.json()
            except Exception:
                body = {}
            target_uuid = str(body.get("target_uuid", "")).strip()
            if target_uuid:
                session = self.bot.bridge.get_session(target_uuid)
                if not session or not session.active:
                    return web.json_response({"ok": False, "error": "Selected call is no longer active."}, status=404)
                if not session.talk_enabled:
                    return web.json_response({"ok": False, "error": "Discord → Caller is muted for the selected call."}, status=409)
                target_label = target_uuid[:8]
            else:
                open_calls = [s for s in self.bot.bridge.get_sessions() if s.active and s.talk_enabled]
                if not open_calls:
                    return web.json_response({"ok": False, "error": "No active call can currently hear Discord."}, status=409)
                target_label = f"{len(open_calls)} open call(s)"
            asyncio.create_task(self._play_soundboard(slot, target_uuid), name=f"web-soundboard-{slot}")
            self.call_history.log_activity("soundboard played", f"slot {slot}: {item['label']} → {target_label}", uuid=target_uuid)
            return web.json_response({"ok": True, "message": f"Playing {item['label']} to {target_label}."})
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _play_soundboard(self, slot: int, target_uuid: str = "") -> None:
        path = self.soundboard.path_for(slot)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(path), "-t", "30",
                "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            pcm, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.error("Web soundboard ffmpeg failed for slot %s: %s", slot, stderr.decode(errors="replace"))
                return
            if not pcm:
                log.warning("Web soundboard slot %s decoded to no PCM", slot)
                return
            pseudo_user_id = -abs(hash(("web-soundboard", slot, time.monotonic_ns())))
            loop = asyncio.get_running_loop()
            next_tick = loop.time()
            log.info("Playing web soundboard slot %s (%d PCM bytes) target=%s", slot, len(pcm), target_uuid or "all")
            for pos in range(0, len(pcm), DISCORD_FRAME_BYTES):
                frame = pcm[pos:pos + DISCORD_FRAME_BYTES]
                if len(frame) < DISCORD_FRAME_BYTES:
                    frame += b"\x00" * (DISCORD_FRAME_BYTES - len(frame))
                if self.bot.bridge.push_web_sound_pcm(pseudo_user_id, frame, target_uuid) == 0:
                    break
                next_tick += 0.020
                delay = next_tick - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_tick = loop.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Could not play web soundboard slot %s", slot)

    async def inbound_register(self, request):
        try:
            call_uuid = str(request.query.get("uuid", "")).strip()
            uuid.UUID(call_uuid)
            raw_number = str(request.query.get("number", "")).strip()
            number = self.bot.ami.normalize_number(raw_number) if raw_number else ""
            contact = self.contacts.find_by_number(number) if number else None
            contact_name = contact.get("name", "") if contact else ""
            self.bot.bridge.prepare_inbound(call_uuid, number, contact_name)
            self.call_history.start_call(
                uuid=call_uuid, direction="inbound", number=number, contact_name=contact_name,
                source="inbound", state="incoming",
            )
            self.call_history.log_activity("inbound registered", contact_name or number or call_uuid[:8], uuid=call_uuid, number=number)
            return web.json_response({"ok": True})
        except (ValueError, AttributeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def join(self, request):
        try:
            vc = await self.bot.bridge.ensure_voice()
            self.call_history.log_activity("Discord voice joined", str(vc.channel.name))
            return web.json_response({"ok": True, "message": f"Joined {vc.channel.name}."})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def leave(self, request):
        await self.bot.bridge.disconnect_voice()
        self.call_history.log_activity("Discord voice left", "")
        return web.json_response({"ok": True, "message": "Disconnected from Discord voice."})

    async def pbx_ping(self, request):
        if not self.bot.ami.configured:
            return web.json_response({"ok": False, "error": "AMI is not configured."}, status=400)
        ok, detail = await asyncio.to_thread(self.bot.ami.ping)
        self.call_history.log_activity("PBX test", "AMI reachable" if ok else self._sanitize_detail(detail))
        return web.json_response({"ok": ok, "message": "Asterisk AMI is reachable." if ok else detail}, status=200 if ok else 502)

    def _prepare_outbound(self, number: str, caller_id: str = "", contact_name: str = "",
                          randomize_caller_id: bool = False, source: str = "manual",
                          retry_of: str = "", retry_index: int = 0) -> tuple[str, str, str, str]:
        """Validate/select the dial parameters and create the visible queue entry.

        This intentionally happens before Discord or AMI I/O. The older web
        endpoint waited for both of those network operations before returning,
        which could leave the Random button apparently hung with no queue card.
        """
        if not self.bot.ami.configured or not self.config.audiosocket_advertise_host:
            raise RuntimeError("AMI or AUDIOSOCKET_ADVERTISE_HOST is not configured.")
        number = self.bot.ami.normalize_number(number)
        caller_id = self._choose_web_caller_id(caller_id, randomize=randomize_caller_id)
        if not contact_name:
            contact = self.contacts.find_by_number(number)
            contact_name = contact.get("name", "") if contact else ""

        status = self.bot.bridge.status_dict()
        if int(status.get("call_count", 0)) + int(status.get("outbound_pending_count", 0)) >= self.config.max_simultaneous_calls:
            raise RuntimeError(f"Maximum simultaneous/pending calls reached ({self.config.max_simultaneous_calls}).")

        call_uuid = self.bot.bridge.prepare_outbound(
            number, caller_id, contact_name, source=source, randomize_caller_id=randomize_caller_id,
            retry_of=retry_of, retry_index=retry_index,
            voicemail_detection_enabled=bool(self._voicemail_detection_enabled),
        )
        self.bot.bridge.update_pending_state(call_uuid, "starting")
        self.call_history.start_call(
            uuid=call_uuid, direction="outbound", number=number, caller_id=caller_id,
            contact_name=contact_name, source=source, state="starting", retry_of=retry_of, retry_index=retry_index,
        )
        self.call_history.log_activity("dial queued", f"{source}: {contact_name or number}", uuid=call_uuid, number=number)
        return number, caller_id, contact_name, call_uuid

    async def _originate_prepared(self, number: str, caller_id: str, contact_name: str, call_uuid: str) -> tuple[bool, str, str]:
        """Finish one already-visible outbound request with bounded I/O waits."""
        try:
            self.bot.bridge.update_pending_state(call_uuid, "joining Discord")
            self.call_history.set_state(call_uuid, "joining Discord")
            # Discord voice connection should normally take well under a second.
            # A hard bound prevents a broken gateway/voice session from wedging the
            # operator UI indefinitely.
            await asyncio.wait_for(self.bot.bridge.ensure_voice(), timeout=12.0)

            if not self.bot.bridge.get_pending(call_uuid):
                return False, "Call was cancelled before originate", call_uuid

            self.bot.bridge.update_pending_state(call_uuid, "sending to PBX")
            self.call_history.set_state(call_uuid, "sending to PBX")
            ami_wait = max(7.0, float(getattr(self.bot.ami, "timeout", 5.0)) + 2.0)
            ok, detail, _ = await asyncio.wait_for(
                asyncio.to_thread(
                    self.bot.ami.originate_to_audiosocket,
                    number, self.config.audiosocket_advertise_host, self.config.audiosocket_port,
                    self.config.ami_dial_context, self.config.ami_dial_timeout_ms, caller_id, call_uuid,
                ),
                timeout=ami_wait,
            )
            if not ok:
                self.bot.bridge.fail_pending(call_uuid, detail)
                outcome = self._failure_outcome(detail)
                self.call_history.fail(call_uuid, outcome=outcome, diagnostic=self._sanitize_detail(detail))
                self.call_history.log_activity("dial failed", f"{outcome}: {self._sanitize_detail(detail)}", uuid=call_uuid, number=number)
                await self._maybe_schedule_redial(call_uuid, outcome, {"uuid": call_uuid, "number": number, "caller_id": caller_id, "contact_name": contact_name})
                return False, detail, call_uuid

            self.bot.bridge.update_pending_state(call_uuid, "dialing / ringing")
            self.call_history.set_state(call_uuid, "dialing / ringing")
            self.contacts.mark_called(number=number)
            return True, detail or "Originate queued", call_uuid
        except asyncio.CancelledError:
            self.bot.bridge.cancel_pending(call_uuid)
            raise
        except asyncio.TimeoutError:
            detail = "Timed out while starting the call (Discord voice or AMI did not respond)"
            self.bot.bridge.fail_pending(call_uuid, detail)
            self.call_history.fail(call_uuid, outcome="timeout", diagnostic=self._sanitize_detail(detail))
            await self._maybe_schedule_redial(call_uuid, "timeout", {"uuid": call_uuid, "number": number, "caller_id": caller_id, "contact_name": contact_name})
            log.error("Outbound call %s timed out while starting", call_uuid)
            return False, detail, call_uuid
        except Exception as exc:
            self.bot.bridge.fail_pending(call_uuid, str(exc))
            self.call_history.fail(call_uuid, outcome="failed", diagnostic=self._sanitize_detail(str(exc)))
            await self._maybe_schedule_redial(call_uuid, "failed", {"uuid": call_uuid, "number": number, "caller_id": caller_id, "contact_name": contact_name})
            log.exception("Outbound call %s failed while starting", call_uuid)
            return False, str(exc), call_uuid

    async def _dial_outbound(self, number: str, caller_id: str = "", contact_name: str = "",
                             randomize_caller_id: bool = False, source: str = "manual",
                             retry_of: str = "", retry_index: int = 0) -> tuple[bool, str, str]:
        number, caller_id, contact_name, call_uuid = self._prepare_outbound(
            number, caller_id, contact_name, randomize_caller_id=randomize_caller_id, source=source,
            retry_of=retry_of, retry_index=retry_index
        )
        return await self._originate_prepared(number, caller_id, contact_name, call_uuid)

    def _queue_web_outbound(self, number: str, caller_id: str = "", contact_name: str = "",
                            randomize_caller_id: bool = False, source: str = "manual",
                            retry_of: str = "", retry_index: int = 0) -> tuple[str, str, str, str]:
        """Create a call queue entry immediately and originate in the background."""
        number, caller_id, contact_name, call_uuid = self._prepare_outbound(
            number, caller_id, contact_name, randomize_caller_id=randomize_caller_id, source=source,
            retry_of=retry_of, retry_index=retry_index
        )
        task = asyncio.create_task(
            self._originate_prepared(number, caller_id, contact_name, call_uuid),
            name=f"web-outbound-{call_uuid[:8]}",
        )
        self._outbound_tasks[call_uuid] = task

        def _done(done_task: asyncio.Task, uid: str = call_uuid) -> None:
            self._outbound_tasks.pop(uid, None)
            if done_task.cancelled():
                return
            try:
                ok, detail, _ = done_task.result()
                if not ok:
                    log.warning("Web outbound %s failed: %s", uid, detail)
            except Exception:
                log.exception("Unhandled web outbound task failure for %s", uid)

        task.add_done_callback(_done)
        return number, caller_id, contact_name, call_uuid

    async def dial(self, request):
        call_uuid = ""
        try:
            body = await request.json()
            number = str(body.get("number", ""))
            caller_id = str(body.get("caller_id", ""))
            randomize_caller_id = bool(body.get("randomize_caller_id", False))
            contact_name = str(body.get("contact_name", "")).strip()
            source = str(body.get("source", "manual") or "manual")[:32]
            number, actual_cid, contact_name, call_uuid = self._queue_web_outbound(
                number, caller_id, contact_name, randomize_caller_id=randomize_caller_id, source=source
            )
            label = contact_name or number
            cid_note = f"{actual_cid} (random pool)" if randomize_caller_id else (actual_cid or "PBX default")
            return web.json_response({
                "ok": True, "message": f"Starting call to {html.escape(label)} using {html.escape(cid_note)}.",
                "uuid": call_uuid, "number": number, "caller_id": actual_cid,
            }, status=202)
        except ValueError as exc:
            if call_uuid:
                self.bot.bridge.cancel_pending(call_uuid)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            if call_uuid:
                self.bot.bridge.cancel_pending(call_uuid)
            log.exception("Web dial failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def dial_random(self, request):
        call_uuid = ""
        try:
            try:
                body = await request.json()
            except Exception:
                body = {}
            # Keep selection and prepare_outbound atomic. A second click can arrive
            # 1.5 seconds later while the first call is still starting; the lock
            # guarantees it sees the first destination/CID as pending and chooses
            # another available pair instead of racing into duplicates.
            async with self._random_dial_lock:
                picked = self._choose_random_call_target()
                number = str(picked.get("number", ""))
                caller_id = str(body.get("caller_id", ""))
                randomize_caller_id = bool(body.get("randomize_caller_id", False))
                contact = self.contacts.find_by_number(number)
                contact_name = (contact.get("name", "") if contact else "") or str(picked.get("label", "")).strip()
                number, actual_cid, contact_name, call_uuid = self._queue_web_outbound(
                    number, caller_id, contact_name, randomize_caller_id=randomize_caller_id, source="random"
                )
            cid_note = f"{actual_cid} (random pool)" if randomize_caller_id else (actual_cid or "PBX default")
            label = contact_name or number
            log.info(
                "Random web call selected destination=%s caller_id=%s uuid=%s",
                number, actual_cid or "PBX default", call_uuid,
            )
            return web.json_response({
                "ok": True,
                "uuid": call_uuid,
                "number": number,
                "caller_id": actual_cid,
                "label": label,
                "message": f"Starting random call to {html.escape(label)} ({html.escape(number)}) using {html.escape(cid_note)}.",
            }, status=202)
        except ValueError as exc:
            if call_uuid:
                self.bot.bridge.cancel_pending(call_uuid)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            if call_uuid:
                self.bot.bridge.cancel_pending(call_uuid)
            log.exception("Random web dial failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def cancel_outbound(self, request):
        call_uuid = request.match_info["uuid"]
        self._cancel_auto_redial(call_uuid)
        pending = self.bot.bridge.get_pending(call_uuid)
        if not pending or pending.get("direction") != "outbound":
            return web.json_response({"ok": False, "error": "Outgoing call is no longer pending."}, status=404)

        task = self._outbound_tasks.get(call_uuid)
        state = str(pending.get("state", ""))
        # Before the AMI phase, cancelling the starter is sufficient and prevents
        # the call from ever being submitted to Asterisk. Once AMI submission has
        # begun, do not rely on cancelling asyncio.to_thread (the worker thread may
        # still complete); instead target the Asterisk channel by our UUID below.
        if task is not None and not task.done() and state in {"starting", "joining Discord"}:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._outbound_tasks.pop(call_uuid, None)
            self.bot.bridge.cancel_pending(call_uuid)
            self.call_history.finish(call_uuid, outcome="cancelled")
            self.call_history.log_activity("outgoing cancelled", "Cancelled before PBX originate", uuid=call_uuid, number=str(pending.get("number", "")))
            return web.json_response({"ok": True, "message": "Outgoing call cancelled before it was sent to PBX."})

        # The AMI channel may take a fraction of a second to appear after an async
        # Originate. Retry briefly so Cancel remains useful during that transition.
        ok = False
        detail = "No matching ringing Asterisk channel found"
        count = 0
        for attempt in range(4):
            ok, detail, count = await asyncio.to_thread(
                self.bot.ami.cancel_originate,
                call_uuid,
                str(pending.get("number", "")),
                self.config.ami_dial_context,
            )
            if ok:
                break
            if attempt < 3:
                await asyncio.sleep(0.25)

        self.bot.bridge.cancel_pending(call_uuid)
        self.call_history.finish(call_uuid, outcome="cancelled", diagnostic=self._sanitize_detail(detail))
        self.call_history.log_activity("outgoing cancelled", detail, uuid=call_uuid, number=str(pending.get("number", "")))
        if ok:
            return web.json_response({"ok": True, "message": f"Outgoing call cancelled ({count} channel(s))."})
        return web.json_response({"ok": True, "message": detail + "; removed from outgoing queue."})

    async def call_routes(self, request):
        body = await request.json()
        uid = request.match_info["uuid"]
        listen = body.get("listen_enabled") if "listen_enabled" in body else None
        talk = body.get("talk_enabled") if "talk_enabled" in body else None
        if not self.bot.bridge.set_call_routes(uid, listen_enabled=listen, talk_enabled=talk):
            return web.json_response({"ok": False, "error": "Call not found."}, status=404)
        self.call_history.log_activity("call routing", f"listen={listen} talk={talk}", uuid=uid)
        return web.json_response({"ok": True, "message": "Call routing updated."})

    async def solo_talk(self, request):
        uid = request.match_info["uuid"]
        if not self.bot.bridge.solo_talk(uid):
            return web.json_response({"ok": False, "error": "Call not found."}, status=404)
        self.call_history.log_activity("solo talk", "Only selected call hears Discord", uuid=uid)
        return web.json_response({"ok": True, "message": "Only this call can hear Discord; you can still hear all unmuted callers."})

    async def focus(self, request):
        uid = request.match_info["uuid"]
        if not self.bot.bridge.focus_call(uid):
            return web.json_response({"ok": False, "error": "Call not found."}, status=404)
        self.call_history.log_activity("focus call", "Other calls muted both directions", uuid=uid)
        return web.json_response({"ok": True, "message": "Focused on this call. Other calls remain connected but muted both directions."})

    async def call_dtmf(self, request):
        uid = request.match_info["uuid"]
        session = self.bot.bridge.get_session(uid)
        if not session or not session.active:
            return web.json_response({"ok": False, "error": "Call not found."}, status=404)
        try:
            body = await request.json()
            digit = str(body.get("digit", ""))[:1]
            # Keep AudioSocket audio-only. AMI PlayDTMF is isolated from the
            # AudioSocket application's lifetime, so a keypad press cannot tear
            # down the bridged call if DTMF forwarding fails.
            ok, detail, channel = await asyncio.to_thread(self.bot.ami.play_dtmf, uid, digit)
            if not ok:
                return web.json_response({"ok": False, "error": self._sanitize_detail(detail)}, status=502)
            self.call_history.log_activity("DTMF", f"Sent {digit}", uuid=uid, number=str(getattr(session, "remote_number", "")))
            return web.json_response({"ok": True, "message": f"Sent DTMF {digit}.", "channel": channel})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": self._sanitize_detail(exc)}, status=502)

    async def call_auto_redial(self, request):
        uid = request.match_info["uuid"]
        session = self.bot.bridge.get_session(uid)
        pending = self.bot.bridge.get_pending(uid)
        row = self.call_history.get_by_uuid(uid)
        if not session and not pending and not row:
            return web.json_response({"ok": False, "error": "Call not found."}, status=404)
        try:
            body = await request.json()
            enabled = bool(body.get("enabled", True))
            if not enabled:
                self._cancel_auto_redial(uid)
                self.call_history.log_activity("auto redial disabled", "", uuid=uid, number=str((row or {}).get("number", "")))
                return web.json_response({"ok": True, "message": "Auto redial disabled."})
            delay = max(1.0, min(300.0, float(body.get("delay", 3) or 3)))
            max_retries = max(1, min(20, int(body.get("max_retries", 3) or 3)))
            current = self._auto_redial.get(uid, {})
            self._auto_redial[uid] = {
                "enabled": True, "delay": delay, "max_retries": max_retries,
                "retries": int(current.get("retries", 0) or 0),
                "randomize_caller_id": bool(body.get("randomize_caller_id", current.get("randomize_caller_id", False))),
                "retry_on": str(body.get("retry_on", current.get("retry_on", "all"))) if str(body.get("retry_on", current.get("retry_on", "all"))) in {"all", "disconnect", "no-answer"} else "all",
                "next_retry_at": float(current.get("next_retry_at", 0) or 0),
                "last_reason": str(current.get("last_reason", "")),
            }
            self.call_history.log_activity("auto redial enabled", f"delay={delay:g}s max={max_retries}", uuid=uid, number=str((row or {}).get("number", "")))
            return web.json_response({"ok": True, "message": f"Auto redial enabled: up to {max_retries} retries, {delay:g}s delay.", "policy": self._auto_redial[uid]})
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def call_notes(self, request):
        uid = request.match_info["uuid"]
        if not self.call_history.get_by_uuid(uid):
            return web.json_response({"ok": False, "error": "Call history entry not found."}, status=404)
        body = await request.json()
        notes = str(body.get("notes", ""))[:2000] if "notes" in body else None
        disposition = str(body.get("disposition", ""))[:80] if "disposition" in body else None
        self.call_history.update_notes(uid, notes=notes, disposition=disposition)
        self.call_history.log_activity("call notes updated", disposition or "notes saved", uuid=uid)
        return web.json_response({"ok": True, "message": "Call notes saved."})

    async def calls_conference(self, request):
        body = await request.json()
        call_uuids = [str(x) for x in body.get("call_uuids", [])][:15]
        ok, message, gid = self.bot.bridge.create_conference(call_uuids)
        if not ok:
            return web.json_response({"ok": False, "error": message}, status=400)
        self.call_history.log_activity("conference created", f"{len(call_uuids)} selected; group {gid[:8]}")
        return web.json_response({"ok": True, "message": message, "conference_group": gid})

    async def call_split(self, request):
        uid = request.match_info["uuid"]
        if not self.bot.bridge.split_call(uid):
            return web.json_response({"ok": False, "error": "Call is not in a conference."}, status=404)
        self.call_history.log_activity("conference split", "Removed call from conference", uuid=uid)
        return web.json_response({"ok": True, "message": "Call removed from conference."})

    async def call_hangup(self, request):
        uid = request.match_info["uuid"]
        self._cancel_auto_redial(uid)
        if not await self.bot.bridge.hangup(uid):
            return web.json_response({"ok": False, "error": "Call not found."}, status=404)
        self.call_history.log_activity("call hangup", "Manual hangup", uuid=uid)
        return web.json_response({"ok": True, "message": "Call disconnected."})

    async def all_talk(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get("enabled", False))
        count = self.bot.bridge.set_all_talk(enabled)
        self.call_history.log_activity("global Discord audio", "unmuted to callers" if enabled else "muted to callers")
        return web.json_response({
            "ok": True,
            "message": f"{'Unmuted' if enabled else 'Muted'} our audio for {count} active call(s).",
        })

    async def all_listen(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get("enabled", False))
        count = self.bot.bridge.set_all_listen(enabled)
        self.call_history.log_activity("global caller audio", "unmuted" if enabled else "muted")
        return web.json_response({"ok": True, "message": f"{'Unmuted' if enabled else 'Muted'} {count} caller(s) into Discord."})

    async def operator_audio(self, request):
        try:
            body = await request.json()
            c2d = body.get("caller_to_discord") if "caller_to_discord" in body else None
            d2c = body.get("discord_to_caller") if "discord_to_caller" in body else None
            chime = body.get("inbound_chime") if "inbound_chime" in body else None
            self.bot.bridge.set_master_gains(caller_to_discord=c2d, discord_to_caller=d2c, inbound_chime=chime)
            self.operator_settings.set_audio_gains(
                caller_to_discord=self.bot.bridge.pbx_to_discord_master_gain,
                discord_to_caller=self.bot.bridge.discord_to_pbx_master_gain,
                inbound_chime=self.bot.bridge.inbound_chime_master_gain,
            )
            self.call_history.log_activity("master audio changed", f"caller→Discord={self.bot.bridge.pbx_to_discord_master_gain:.2f}; Discord→caller={self.bot.bridge.discord_to_pbx_master_gain:.2f}; chime={self.bot.bridge.inbound_chime_master_gain:.2f}")
            return web.json_response({
                "ok": True, "message": "Master audio levels updated.",
                "caller_to_discord": self.bot.bridge.pbx_to_discord_master_gain,
                "discord_to_caller": self.bot.bridge.discord_to_pbx_master_gain,
                "inbound_chime": self.bot.bridge.inbound_chime_master_gain,
            })
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def all_routes(self, request):
        self.bot.bridge.enable_all_routes()
        self.call_history.log_activity("all call routes opened", "Both directions enabled for all active calls")
        return web.json_response({"ok": True, "message": "All active calls can hear Discord and be heard in Discord."})

    async def hangup_all(self, request):
        for uid in list(self._auto_redial):
            self._cancel_auto_redial(uid)
        n = await self.bot.bridge.hangup_all()
        self.call_history.log_activity("hangup all", f"Disconnected {n} call(s)")
        return web.json_response({"ok": True, "message": f"Disconnected {n} call(s)."})
