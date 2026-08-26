from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path
from typing import Iterable

import yaml


_NANP_RE = re.compile(r"^1\d{10}$")
_PHONE_FIND_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s().-]*)?(?:\(?\d{3}\)?[\s.-]*)\d{3}[\s.-]*\d{4}(?!\d)"
)


def normalize_caller_id(raw: str) -> str:
    """Normalize a NANP caller ID to 1XXXXXXXXXX."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("phone number is empty")
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        digits = "1" + digits
    if not _NANP_RE.fullmatch(digits):
        raise ValueError("caller ID must be a 10-digit NANP number or 1 + 10 digits")
    return digits


def extract_bulk_candidates(raw: str) -> tuple[list[str], list[str]]:
    """Extract and normalize phone-like values from flexible pasted text."""
    text = str(raw or "")
    matches = list(_PHONE_FIND_RE.finditer(text))
    valid: list[str] = []
    seen: set[str] = set()
    for match in matches:
        try:
            number = normalize_caller_id(match.group(0))
        except ValueError:
            continue
        if number not in seen:
            seen.add(number)
            valid.append(number)

    chars = list(text)
    for match in matches:
        for i in range(match.start(), match.end()):
            chars[i] = " "
    residual = "".join(chars)
    invalid: list[str] = []
    invalid_seen: set[str] = set()
    for token in re.split(r"[\n,;\t]+", residual):
        token = token.strip()
        if not token or not any(ch.isdigit() for ch in token):
            continue
        subparts = token.split()
        handled = False
        if len(subparts) > 1:
            for sub in subparts:
                if not any(ch.isdigit() for ch in sub):
                    continue
                try:
                    number = normalize_caller_id(sub)
                except ValueError:
                    if sub not in invalid_seen:
                        invalid_seen.add(sub)
                        invalid.append(sub)
                else:
                    if number not in seen:
                        seen.add(number)
                        valid.append(number)
                handled = True
        if handled:
            continue
        try:
            number = normalize_caller_id(token)
        except ValueError:
            if token not in invalid_seen:
                invalid_seen.add(token)
                invalid.append(token)
        else:
            if number not in seen:
                seen.add(number)
                valid.append(number)

    return sorted(valid), invalid


class CallerIdPoolStore:
    """Persistent caller-ID pool with an in-memory cache.

    Large pools used to be reparsed from YAML on every /api/status request. With
    thousands of entries that blocked aiohttp's event loop and made dial requests
    time out. v1.7 loads YAML once, writes atomically, and only reparses if the
    file was modified externally.
    """

    def __init__(self, path: str, seed_numbers: Iterable[str] = ()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._mtime_ns = -1

        if self.path.exists():
            self._items = self._read_file()
            # Normalize once at startup so hand-edited YAML stays deterministic.
            self._commit(self._items)
        else:
            entries: list[dict] = []
            for raw in seed_numbers:
                try:
                    number = normalize_caller_id(raw)
                except ValueError:
                    continue
                if not any(x["number"] == number for x in entries):
                    entries.append(self._new_entry(number, label="Imported from .env"))
            self._commit(entries)

    @staticmethod
    def _new_entry(number: str, label: str = "") -> dict:
        return {
            "id": uuid.uuid4().hex,
            "number": normalize_caller_id(number),
            "label": str(label or "").strip()[:80],
            "enabled": True,
        }

    @staticmethod
    def _normalize_items(items: Iterable[dict]) -> list[dict]:
        normalized: list[dict] = []
        seen: set[str] = set()
        for raw in items:
            if isinstance(raw, str):
                raw = {"number": raw}
            if not isinstance(raw, dict):
                continue
            try:
                number = normalize_caller_id(raw.get("number", ""))
            except ValueError:
                continue
            if number in seen:
                continue
            seen.add(number)
            normalized.append({
                "id": str(raw.get("id") or uuid.uuid4().hex),
                "number": number,
                "label": str(raw.get("label", "")).strip()[:80],
                "enabled": bool(raw.get("enabled", True)),
            })
        normalized.sort(key=lambda x: x["number"])
        return normalized

    def _read_file(self) -> list[dict]:
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            payload = {}
        raw_items = payload.get("caller_ids", []) if isinstance(payload, dict) else []
        return self._normalize_items(raw_items if isinstance(raw_items, list) else [])

    def _remember_mtime(self) -> None:
        try:
            self._mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime_ns = -1

    def _refresh_if_external_change(self) -> None:
        """Cheap stat check; YAML is reparsed only when another process edits it."""
        try:
            current = self.path.stat().st_mtime_ns
        except OSError:
            return
        if current != self._mtime_ns:
            self._items = self._read_file()
            self._mtime_ns = current

    def _commit(self, items: Iterable[dict]) -> None:
        normalized = self._normalize_items(items)
        payload = {"caller_ids": normalized}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(self.path)
        self._items = normalized
        self._remember_mtime()

    def list(self) -> list[dict]:
        with self._lock:
            self._refresh_if_external_change()
            return [dict(x) for x in self._items]

    def page(self, *, offset: int = 0, limit: int = 100, query: str = "") -> tuple[list[dict], int]:
        with self._lock:
            self._refresh_if_external_change()
            q = str(query or "").strip().lower()
            items = self._items
            if q:
                items = [x for x in items if q in x["number"].lower() or q in x.get("label", "").lower()]
            total = len(items)
            offset = max(0, int(offset or 0))
            limit = max(1, min(250, int(limit or 100)))
            return [dict(x) for x in items[offset: offset + limit]], total

    def counts(self) -> tuple[int, int]:
        with self._lock:
            self._refresh_if_external_change()
            return len(self._items), sum(1 for x in self._items if x.get("enabled"))

    def enabled_numbers(self) -> list[str]:
        with self._lock:
            self._refresh_if_external_change()
            return [x["number"] for x in self._items if x.get("enabled")]

    def preview_bulk(self, raw: str) -> dict:
        numbers, invalid = extract_bulk_candidates(raw)
        with self._lock:
            self._refresh_if_external_change()
            existing = {x["number"] for x in self._items}
        duplicates = [n for n in numbers if n in existing]
        addable = [n for n in numbers if n not in existing]
        return {"valid": numbers, "addable": addable, "duplicates": duplicates, "invalid": invalid}

    def add_bulk(self, raw: str) -> dict:
        numbers, invalid = extract_bulk_candidates(raw)
        with self._lock:
            self._refresh_if_external_change()
            existing = {x["number"] for x in self._items}
            duplicates = [n for n in numbers if n in existing]
            addable = [n for n in numbers if n not in existing]
            items = list(self._items)
            items.extend(self._new_entry(number) for number in addable)
            self._commit(items)
            return {
                "valid": numbers,
                "addable": addable,
                "duplicates": duplicates,
                "invalid": invalid,
                "added": len(addable),
            }

    def remove_bulk(self, raw: str) -> dict:
        numbers, invalid = extract_bulk_candidates(raw)
        with self._lock:
            self._refresh_if_external_change()
            requested = set(numbers)
            existing = {x["number"] for x in self._items}
            removed_numbers = sorted(requested & existing)
            missing = sorted(requested - existing)
            if removed_numbers:
                self._commit([x for x in self._items if x["number"] not in requested])
            return {
                "valid": numbers,
                "removed_numbers": removed_numbers,
                "missing": missing,
                "invalid": invalid,
                "removed": len(removed_numbers),
            }

    def update(self, entry_id: str, *, enabled: bool | None = None, label: str | None = None) -> dict | None:
        with self._lock:
            self._refresh_if_external_change()
            items = [dict(x) for x in self._items]
            found = None
            for item in items:
                if item["id"] != entry_id:
                    continue
                if enabled is not None:
                    item["enabled"] = bool(enabled)
                if label is not None:
                    item["label"] = str(label).strip()[:80]
                found = dict(item)
                break
            if not found:
                return None
            self._commit(items)
            return found

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            self._refresh_if_external_change()
            kept = [x for x in self._items if x["id"] != entry_id]
            if len(kept) == len(self._items):
                return False
            self._commit(kept)
            return True

    def yaml_text(self) -> str:
        with self._lock:
            self._refresh_if_external_change()
            payload = {
                "caller_ids": [
                    {"number": x["number"], "label": x["label"], "enabled": bool(x["enabled"])}
                    for x in self._items
                ]
            }
            return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
