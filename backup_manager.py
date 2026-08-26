from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath


class BackupManager:
    """Versioned data backups with SQLite-consistent snapshots and safe restart restore."""

    FORMAT = 3
    PENDING_RESTORE = "restore-pending.json"

    def __init__(self, data_dir: str = "/app/data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.data_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sqlite_backup(src: Path, dst: Path) -> None:
        """Create a transactionally consistent SQLite copy, including WAL state."""
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=20)
        target = sqlite3.connect(dst, timeout=20)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def create(self, label: str = "manual", include_secrets: bool = False) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c for c in label if c.isalnum() or c in "-_ ").strip().replace(" ", "-")[:40] or "backup"
        out = self.backup_dir / f"pbx-{stamp}-{safe}.zip"
        exclude = {"master.key", self.PENDING_RESTORE, "restore-result.json"}
        if not include_secrets:
            exclude.add("secrets.enc.json")

        with tempfile.TemporaryDirectory(prefix="pbx-backup-") as td:
            temp = Path(td)
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                manifest = {
                    "format": self.FORMAT,
                    "created_at": time.time(),
                    "label": label,
                    "include_secrets": bool(include_secrets),
                }
                zf.writestr("manifest.json", json.dumps(manifest, indent=2))
                for path in self.data_dir.rglob("*"):
                    if not path.is_file() or self.backup_dir in path.parents or path.name in exclude:
                        continue
                    rel = path.relative_to(self.data_dir)
                    archive_name = f"data/{rel.as_posix()}"
                    if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                        snap = temp / (path.name + ".snapshot")
                        try:
                            self._sqlite_backup(path, snap)
                            zf.write(snap, archive_name)
                            continue
                        except sqlite3.DatabaseError:
                            # Non-SQLite files occasionally use .db; ordinary copy is safe.
                            pass
                    zf.write(path, archive_name)
        return out

    def list(self, limit: int = 30) -> list[dict]:
        items = []
        for p in sorted(self.backup_dir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            st = p.stat()
            valid = True
            try:
                self.validate(p)
            except Exception:
                valid = False
            items.append({"name": p.name, "size": st.st_size, "created_at": st.st_mtime, "valid": valid})
        return items

    def validate(self, path: str | Path) -> dict:
        path = Path(path)
        if not path.is_file() or path.parent.resolve() != self.backup_dir.resolve():
            raise ValueError("backup does not exist")
        with zipfile.ZipFile(path, "r") as zf:
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception as exc:
                raise ValueError("backup manifest is missing or invalid") from exc
            if int(manifest.get("format", 0) or 0) != self.FORMAT:
                raise ValueError("unsupported backup format")
            for info in zf.infolist():
                name = PurePosixPath(info.filename)
                if info.filename == "manifest.json":
                    continue
                if not info.filename.startswith("data/") or name.is_absolute() or ".." in name.parts:
                    raise ValueError(f"unsafe backup member: {info.filename}")
        return manifest

    def queue_restore(self, name: str) -> dict:
        path = self.backup_dir / Path(name).name
        manifest = self.validate(path)
        marker = self.data_dir / self.PENDING_RESTORE
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps({"name": path.name, "queued_at": time.time()}, indent=2), encoding="utf-8")
        os.replace(tmp, marker)
        return manifest

    def apply_pending_restore(self) -> dict | None:
        """Apply a queued restore before application databases are opened.

        The backups folder and encryption master key are always preserved. Secrets are
        restored only when the selected backup explicitly contains them, which makes
        same-install secret restores possible without ever exporting master.key.
        """
        marker = self.data_dir / self.PENDING_RESTORE
        if not marker.is_file():
            return None
        info = json.loads(marker.read_text(encoding="utf-8"))
        path = self.backup_dir / Path(str(info.get("name", ""))).name
        manifest = self.validate(path)
        include_secrets = bool(manifest.get("include_secrets", False))
        result_path = self.data_dir / "restore-result.json"

        parent = self.data_dir.parent
        with tempfile.TemporaryDirectory(prefix="pbx-restore-", dir=parent) as td:
            stage = Path(td)
            with zipfile.ZipFile(path, "r") as zf:
                for member in zf.infolist():
                    if member.filename == "manifest.json" or member.is_dir():
                        continue
                    posix = PurePosixPath(member.filename)
                    rel = Path(*posix.parts[1:])  # drop data/
                    dest = stage / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member, "r") as src, dest.open("wb") as dst:
                        shutil.copyfileobj(src, dst)

            preserve = {"master.key", self.PENDING_RESTORE, "restore-result.json"}
            if not include_secrets:
                preserve.add("secrets.enc.json")
            # Remove current application data not intentionally preserved. Backups stay.
            for item in list(self.data_dir.iterdir()):
                if item.name == "backups" or item.name in preserve:
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink(missing_ok=True)
            # Overlay staged backup data.
            for src in stage.rglob("*"):
                rel = src.relative_to(stage)
                dst = self.data_dir / rel
                if src.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

        marker.unlink(missing_ok=True)
        result = {"ok": True, "name": path.name, "restored_at": time.time(), "include_secrets": include_secrets}
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def restore_status(self) -> dict:
        pending = self.data_dir / self.PENDING_RESTORE
        result = self.data_dir / "restore-result.json"
        out = {"pending": False, "last": None}
        if pending.is_file():
            try:
                out["pending"] = True
                out["pending_restore"] = json.loads(pending.read_text(encoding="utf-8"))
            except Exception:
                out["pending"] = True
        if result.is_file():
            try:
                out["last"] = json.loads(result.read_text(encoding="utf-8"))
            except Exception:
                pass
        return out

    def prune(self, keep: int = 20) -> int:
        files = sorted(self.backup_dir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
        removed = 0
        for p in files[max(1, keep):]:
            p.unlink(missing_ok=True)
            removed += 1
        return removed

    def startup_snapshot_once(self, version: str) -> Path | None:
        marker = self.backup_dir / f".startup-{version}"
        if marker.exists():
            return None
        out = self.create(f"pre-{version}", include_secrets=False)
        marker.write_text(str(time.time()))
        self.prune(20)
        return out
