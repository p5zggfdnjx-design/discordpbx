from __future__ import annotations

import logging
from pathlib import Path

from call_history import CallHistoryStore

log = logging.getLogger("discord-pbx.history-mirror")


class MirroredCallHistoryStore:
    """Use the consolidated app DB as primary while maintaining a rollback mirror."""

    MUTATING_METHODS = {
        "start_call",
        "set_state",
        "set_workspaces",
        "set_answered_by",
        "connected",
        "finish",
        "fail",
        "update_notes",
        "event",
        "log_activity",
        "migrate_legacy_workspace",
    }

    def __init__(self, primary: CallHistoryStore, mirror: CallHistoryStore):
        self.primary = primary
        self.mirror = mirror
        self.path = primary.path
        self.mirror_path = mirror.path

    def __getattr__(self, name):
        target = getattr(self.primary, name)
        if not callable(target) or name not in self.MUTATING_METHODS:
            return target

        def mirrored(*args, **kwargs):
            result = target(*args, **kwargs)
            try:
                getattr(self.mirror, name)(*args, **kwargs)
            except Exception:
                log.exception("Could not mirror call-history mutation %s", name)
            return result

        return mirrored


def apply() -> None:
    try:
        import webui_v3
    except ModuleNotFoundError:
        import webui as webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_history_mirror_applied", False):
        return

    original_init = cls.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if not bool(getattr(self, "_database_consolidated", False)):
            return
        data_dir = Path(self.config.data_dir)
        mirror_path = data_dir / "call_history.sqlite3"
        try:
            primary = self.call_history
            mirror = CallHistoryStore(str(mirror_path))
            self.call_history = MirroredCallHistoryStore(primary, mirror)
        except Exception:
            log.exception("Could not enable call-history rollback mirror")

    cls.__init__ = init
    cls._history_mirror_applied = True
