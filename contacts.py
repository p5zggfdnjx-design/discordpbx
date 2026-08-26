from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path


def canonical_contact_number(value: str) -> str:
    raw = str(value or "").strip()
    compact = re.sub(r"[\s().-]", "", raw)
    if compact.startswith("+1") and compact[2:].isdigit() and len(compact) == 12:
        return compact[1:]
    if compact.isdigit() and len(compact) == 10:
        return "1" + compact
    return compact or raw


def phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


class ContactsStore:
    """Persistent contact/speed-dial store with transparent v0.x migration."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])
        self._migrate_numbers()

    def _migrate_numbers(self) -> None:
        """Persist legacy 10-digit contact numbers as 1XXXXXXXXXX."""
        with self._lock:
            contacts = self._read()
            changed = False
            for contact in contacts:
                old = str(contact.get("number", ""))
                new = canonical_contact_number(old)
                if new != old:
                    contact["number"] = new
                    changed = True
            if changed:
                self._write(contacts)

    def migrate_legacy_workspace(self, workspace_id: str) -> int:
        """Assign pre-v3 unscoped contacts to the initial/default workspace.

        v2 contacts had no workspace metadata. Treating those rows as global would
        leak a personal address book to every future Discord workspace, while
        leaving them blank makes them disappear from workspace-scoped queries.
        """
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            return 0
        changed = 0
        with self._lock:
            contacts = self._read()
            for contact in contacts:
                scope = str(contact.get("scope", "workspace") or "workspace")
                if scope != "global" and not str(contact.get("workspace_id", "") or ""):
                    contact["workspace_id"] = workspace_id
                    contact["scope"] = "workspace"
                    contact["updated_at"] = float(contact.get("updated_at", 0) or time.time())
                    changed += 1
            if changed:
                self._write(contacts)
        return changed

    def _read(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            return [x for x in raw if isinstance(x, dict)]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _write(self, contacts: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(contacts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _public(contact: dict) -> dict:
        created = float(contact.get("created_at", 0) or 0)
        updated = float(contact.get("updated_at", created) or created)
        last_called = float(contact.get("last_called_at", 0) or 0)
        try:
            call_count = max(0, int(contact.get("call_count", 0) or 0))
        except (TypeError, ValueError):
            call_count = 0
        try:
            quick_order = max(0, int(contact.get("quick_order", 0) or 0))
        except (TypeError, ValueError):
            quick_order = 0
        return {
            "id": str(contact.get("id", "")),
            "name": str(contact.get("name", "")),
            "number": canonical_contact_number(str(contact.get("number", ""))),
            "group": str(contact.get("group", "")),
            "notes": str(contact.get("notes", "")),
            "favorite": bool(contact.get("favorite", False)),
            "bypass_voicemail_detection": bool(contact.get("bypass_voicemail_detection", False)),
            "created_at": created,
            "updated_at": updated,
            "call_count": call_count,
            "last_called_at": last_called,
            "quick_order": quick_order,
            "workspace_id": str(contact.get("workspace_id", "")),
            "scope": str(contact.get("scope", "workspace") or "workspace"),
            "tags": [str(x) for x in contact.get("tags", []) if str(x)][:20] if isinstance(contact.get("tags", []), list) else [],
        }

    def list(self, workspace_id: str = "", include_global: bool = True) -> list[dict]:
        with self._lock:
            contacts = [self._public(x) for x in self._read()]
        if workspace_id:
            contacts = [c for c in contacts if c.get("workspace_id") == workspace_id or (include_global and c.get("scope") == "global")]
        return sorted(
            contacts,
            key=lambda x: (
                not x["favorite"],
                -float(x.get("last_called_at", 0) or 0),
                x["name"].lower(),
                x["number"],
            ),
        )

    def get(self, contact_id: str) -> dict | None:
        with self._lock:
            contact = next((x for x in self._read() if x.get("id") == contact_id), None)
        return self._public(contact) if contact else None

    def find_by_number(self, number: str, workspace_id: str = "") -> dict | None:
        key = phone_key(number)
        if not key:
            return None
        with self._lock:
            for contact in self._read():
                public = self._public(contact)
                if workspace_id and not (public.get("workspace_id") == workspace_id or public.get("scope") == "global"):
                    continue
                if phone_key(str(contact.get("number", ""))) == key:
                    return public
        return None

    @staticmethod
    def _assert_unique_number(contacts: list[dict], number: str, ignore_id: str = "", workspace_id: str = "", scope: str = "workspace") -> None:
        key = phone_key(number)
        if not key:
            return
        for contact in contacts:
            if ignore_id and str(contact.get("id", "")) == ignore_id:
                continue
            if phone_key(str(contact.get("number", ""))) != key:
                continue
            other_ws = str(contact.get("workspace_id", ""))
            other_scope = str(contact.get("scope", "workspace") or "workspace")
            # Global contacts collide with every workspace. Workspace contacts only
            # collide inside the same workspace.
            if scope == "global" or other_scope == "global" or other_ws == workspace_id:
                raise ValueError("another contact already uses this phone number in this workspace")

    def create(self, *, name: str, number: str, group: str = "", notes: str = "", favorite: bool = False, bypass_voicemail_detection: bool = False, workspace_id: str = "", scope: str = "workspace", tags=None) -> dict:
        now = time.time()
        number = canonical_contact_number(number)
        contact = {
            "id": str(uuid.uuid4()), "name": name, "number": number,
            "group": group, "notes": notes, "favorite": bool(favorite),
            "bypass_voicemail_detection": bool(bypass_voicemail_detection),
            "created_at": now, "updated_at": now, "call_count": 0, "last_called_at": 0, "quick_order": 0,
            "workspace_id": str(workspace_id or ""), "scope": "global" if scope == "global" else "workspace",
            "tags": [str(x)[:60] for x in (tags or []) if str(x)][:20],
        }
        with self._lock:
            contacts = self._read()
            self._assert_unique_number(contacts, number, workspace_id=contact["workspace_id"], scope=contact["scope"])
            contacts.append(contact)
            self._write(contacts)
        return self._public(contact)

    def update(self, contact_id: str, *, name: str, number: str, group: str = "", notes: str = "", favorite: bool = False, bypass_voicemail_detection: bool = False, workspace_id: str | None = None, scope: str | None = None, tags=None) -> dict | None:
        number = canonical_contact_number(number)
        with self._lock:
            contacts = self._read()
            existing = next((c for c in contacts if c.get("id") == contact_id), None)
            if not existing:
                return None
            new_ws = str(existing.get("workspace_id", "") if workspace_id is None else workspace_id)
            new_scope = str(existing.get("scope", "workspace") if scope is None else scope)
            new_scope = "global" if new_scope == "global" else "workspace"
            self._assert_unique_number(contacts, number, ignore_id=contact_id, workspace_id=new_ws, scope=new_scope)
            for contact in contacts:
                if contact.get("id") == contact_id:
                    contact.update({
                        "name": name, "number": number, "group": group, "notes": notes,
                        "favorite": bool(favorite), "bypass_voicemail_detection": bool(bypass_voicemail_detection), "updated_at": time.time(),
                        "workspace_id": new_ws, "scope": new_scope,
                    })
                    if tags is not None:
                        contact["tags"] = [str(x)[:60] for x in tags if str(x)][:20]
                    contact.pop("caller_id", None)
                    self._write(contacts)
                    return self._public(contact)
        return None

    def mark_called(self, *, contact_id: str = "", number: str = "", workspace_id: str = "") -> dict | None:
        key = phone_key(number)
        with self._lock:
            contacts = self._read()
            for contact in contacts:
                match_id = bool(contact_id and contact.get("id") == contact_id)
                public = self._public(contact)
                allowed_scope = not workspace_id or public.get("workspace_id") == workspace_id or public.get("scope") == "global"
                match_num = bool(key and allowed_scope and phone_key(str(contact.get("number", ""))) == key)
                if not (match_id or match_num):
                    continue
                try:
                    count = int(contact.get("call_count", 0) or 0)
                except (TypeError, ValueError):
                    count = 0
                now = time.time()
                contact["call_count"] = max(0, count) + 1
                contact["last_called_at"] = now
                contact["updated_at"] = now
                self._write(contacts)
                return self._public(contact)
        return None

    def reorder(self, contact_ids: list[str], workspace_id: str = "") -> int:
        """Persist Quick Dial order without mutating contacts owned by other workspaces."""
        wanted = [str(x) for x in contact_ids if x]
        with self._lock:
            contacts = self._read()
            by_id = {str(c.get("id", "")): c for c in contacts}
            if workspace_id:
                eligible = [
                    str(c.get("id", "")) for c in contacts
                    if str(c.get("workspace_id", "")) == workspace_id and str(c.get("scope", "workspace") or "workspace") == "workspace"
                ]
            else:
                eligible = list(by_id)
            eligible_set = set(eligible)
            ordered = [cid for cid in wanted if cid in eligible_set]
            tail = [cid for cid in eligible if cid not in set(ordered)]
            now = time.time()
            for pos, cid in enumerate(ordered + tail, start=1):
                by_id[cid]["quick_order"] = pos
                by_id[cid]["updated_at"] = now
            if ordered:
                self._write(contacts)
            return len(ordered)

    def delete(self, contact_id: str) -> bool:
        with self._lock:
            contacts = self._read()
            filtered = [x for x in contacts if x.get("id") != contact_id]
            if len(filtered) == len(contacts):
                return False
            self._write(filtered)
            return True
