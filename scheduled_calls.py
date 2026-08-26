from __future__ import annotations

import json
import os
import threading
import time
import uuid
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _canonical_number(value: str) -> str:
    raw = str(value or "").strip()
    compact = re.sub(r"[\s().-]", "", raw)
    if compact.startswith("+1") and compact[2:].isdigit() and len(compact) == 12:
        return compact[1:]
    if compact.isdigit() and len(compact) == 10:
        return "1" + compact
    return compact or raw


class ScheduledCallStore:
    """Persistent low-volume scheduled call rules.

    Supports one-time, daily, and weekly schedules. Recurring schedules are
    advanced to their next occurrence after each attempt instead of being
    consumed. The queue is intentionally bounded and does not implement bulk
    campaign dialing.
    """

    VALID_RECURRENCES = {"once", "daily", "weekly"}

    def __init__(self, path: str, max_pending: int = 20):
        self.path = Path(path)
        self.max_pending = max(1, int(max_pending))
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text()) if self.path.exists() else []
                self._items = raw if isinstance(raw, list) else []
            except Exception:
                self._items = []

            changed = False
            for item in self._items:
                # Migrate v0.8 one-shot entries in place.
                if "recurrence" not in item:
                    item["recurrence"] = "once"
                    item["timezone"] = "UTC"
                    item["local_time"] = ""
                    item["weekdays"] = []
                    item["last_run_at"] = item.get("completed_at")
                    item["last_result"] = item.get("detail", "")
                    item["runs"] = 1 if item.get("completed_at") else 0
                    changed = True
                if "randomize_caller_id" not in item:
                    item["randomize_caller_id"] = False
                    changed = True
                old_number = str(item.get("number", ""))
                new_number = _canonical_number(old_number)
                if new_number != old_number:
                    item["number"] = new_number
                    changed = True
                old_cid = str(item.get("caller_id", ""))
                new_cid = _canonical_number(old_cid) if old_cid else ""
                if new_cid != old_cid:
                    item["caller_id"] = new_cid
                    changed = True
                if "workspace_id" not in item:
                    item["workspace_id"] = ""
                    changed = True
                if "created_by_user_id" not in item:
                    item["created_by_user_id"] = ""
                    item["created_by_name"] = ""
                    changed = True
                if item.get("status") == "running":
                    item["status"] = "pending"
                    item["detail"] = "Recovered after restart"
                    changed = True
            if changed:
                self._save_locked()

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._items, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    @staticmethod
    def _validate_timezone(name: str) -> str:
        name = (name or "UTC").strip()
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return name

    @staticmethod
    def _validate_local_time(value: str) -> str:
        value = str(value or "").strip()
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("time must use HH:MM") from exc
        return parsed.strftime("%H:%M")

    @staticmethod
    def _validate_weekdays(values) -> list[int]:
        try:
            days = sorted({int(x) for x in (values or [])})
        except Exception as exc:
            raise ValueError("weekdays are invalid") from exc
        if any(x < 0 or x > 6 for x in days):
            raise ValueError("weekdays must be between 0 and 6")
        return days

    @classmethod
    def _next_recurring_run(cls, *, recurrence: str, timezone_name: str, local_time: str,
                            weekdays: list[int], after_ts: float) -> float:
        tz = ZoneInfo(timezone_name)
        hour, minute = (int(x) for x in local_time.split(":"))
        after_local = datetime.fromtimestamp(float(after_ts), tz)

        if recurrence == "daily":
            candidate = after_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate.timestamp() <= after_ts + 0.5:
                candidate = candidate + timedelta(days=1)
            return candidate.timestamp()

        if recurrence == "weekly":
            if not weekdays:
                raise ValueError("select at least one weekday for a weekly schedule")
            for delta in range(0, 8):
                day = (after_local + timedelta(days=delta)).date()
                if day.weekday() not in weekdays:  # Monday=0 ... Sunday=6
                    continue
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                if candidate.timestamp() > after_ts + 0.5:
                    return candidate.timestamp()
            raise ValueError("could not calculate next weekly occurrence")

        raise ValueError("recurrence is not recurring")

    def migrate_legacy_workspace(self, workspace_id: str) -> int:
        """Attach pre-v3 schedules with no workspace to the initial workspace.

        v2 schedules were installation-global because only one Discord guild existed.
        Leaving them blank after a multi-tenant upgrade would make them visible from
        every workspace. This migration is idempotent and only fills blank values.
        """
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            return 0
        changed = 0
        with self._lock:
            for item in self._items:
                if not str(item.get("workspace_id") or "").strip():
                    item["workspace_id"] = workspace_id
                    changed += 1
            if changed:
                self._save_locked()
        return changed

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(x) for x in sorted(
                self._items,
                key=lambda x: (0 if x.get("status") == "pending" else 1, float(x.get("run_at", 0) or 0)),
            )]

    def create(self, *, number: str, caller_id: str, recurrence: str,
               timezone_name: str = "UTC", local_time: str = "",
               weekdays=None, run_at: float = 0, contact_name: str = "",
               randomize_caller_id: bool = False, workspace_id: str = "",
               created_by_user_id: str = "", created_by_name: str = "") -> dict:
        now = time.time()
        number = _canonical_number(number)
        caller_id = _canonical_number(caller_id) if caller_id else ""
        recurrence = str(recurrence or "weekly").strip().lower()
        if recurrence not in self.VALID_RECURRENCES:
            raise ValueError("repeat must be once, daily, or weekly")

        timezone_name = self._validate_timezone(timezone_name)
        days = self._validate_weekdays(weekdays)

        if recurrence == "once":
            run_at = float(run_at or 0)
            if run_at < now + 10:
                raise ValueError("scheduled time must be at least 10 seconds in the future")
            local_time = ""
            days = []
        else:
            local_time = self._validate_local_time(local_time)
            if recurrence == "weekly" and not days:
                raise ValueError("select at least one weekday")
            run_at = self._next_recurring_run(
                recurrence=recurrence,
                timezone_name=timezone_name,
                local_time=local_time,
                weekdays=days,
                after_ts=now,
            )

        with self._lock:
            pending = sum(1 for x in self._items if x.get("status") == "pending")
            if pending >= self.max_pending:
                raise ValueError(f"maximum of {self.max_pending} active schedules reached")
            item = {
                "id": str(uuid.uuid4()),
                "number": number,
                "caller_id": caller_id,
                "randomize_caller_id": bool(randomize_caller_id),
                "contact_name": contact_name,
                "recurrence": recurrence,
                "timezone": timezone_name,
                "local_time": local_time,
                "weekdays": days,
                "run_at": float(run_at),
                "created_at": now,
                "status": "pending",
                "detail": "",
                "last_run_at": None,
                "last_result": "",
                "call_uuid": "",
                "runs": 0,
                "completed_at": None,
                "workspace_id": str(workspace_id or ""),
                "created_by_user_id": str(created_by_user_id or ""),
                "created_by_name": str(created_by_name or "")[:120],
            }
            self._items.append(item)
            self._trim_locked()
            self._save_locked()
            return dict(item)

    def _trim_locked(self) -> None:
        inactive = [x for x in self._items if x.get("status") != "pending"]
        if len(inactive) > 40:
            keep_ids = {x["id"] for x in inactive[-40:]}
            self._items = [x for x in self._items if x.get("status") == "pending" or x.get("id") in keep_ids]

    def cancel(self, item_id: str) -> bool:
        with self._lock:
            for item in self._items:
                if item.get("id") == item_id and item.get("status") in {"pending", "running"}:
                    item["status"] = "cancelled"
                    item["completed_at"] = time.time()
                    item["detail"] = "Cancelled"
                    self._save_locked()
                    return True
        return False

    def delete(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [x for x in self._items if x.get("id") != item_id]
            if len(self._items) != before:
                self._save_locked()
                return True
        return False

    def claim_due(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else float(now)
        due: list[dict] = []
        with self._lock:
            for item in self._items:
                if item.get("status") == "pending" and float(item.get("run_at", 0)) <= now:
                    item["status"] = "running"
                    item["detail"] = "Dialing"
                    due.append(dict(item))
            if due:
                self._save_locked()
        return due

    def finish_occurrence(self, item_id: str, *, ok: bool, detail: str = "", call_uuid: str = "",
                          missed: bool = False) -> None:
        now = time.time()
        with self._lock:
            for item in self._items:
                if item.get("id") != item_id:
                    continue
                recurrence = item.get("recurrence", "once")
                scheduled_for = float(item.get("run_at", now) or now)
                item["last_run_at"] = scheduled_for
                item["last_result"] = detail or ("Queued" if ok else "Failed")
                item["call_uuid"] = call_uuid
                item["runs"] = int(item.get("runs", 0) or 0) + (0 if missed else 1)

                if recurrence in {"daily", "weekly"}:
                    try:
                        item["run_at"] = self._next_recurring_run(
                            recurrence=recurrence,
                            timezone_name=item.get("timezone", "UTC"),
                            local_time=item.get("local_time", "00:00"),
                            weekdays=self._validate_weekdays(item.get("weekdays", [])),
                            after_ts=max(now, scheduled_for + 1),
                        )
                        item["status"] = "pending"
                        item["detail"] = "Last: " + item["last_result"]
                        item["completed_at"] = None
                    except Exception as exc:
                        item["status"] = "failed"
                        item["detail"] = f"Could not reschedule: {exc}"
                        item["completed_at"] = now
                else:
                    item["status"] = "dialed" if ok else ("missed" if missed else "failed")
                    item["detail"] = detail
                    item["completed_at"] = now
                self._trim_locked()
                self._save_locked()
                return
