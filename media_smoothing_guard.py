from __future__ import annotations

"""Low-latency media smoothing for live DiscordPBX calls.

v3.4.3 removed the catastrophic Discord receive-worker/reconnect failures.  Live
production traces then exposed a smaller but audible class of cut-outs where both
Asterisk WebSocket media and Discord voice stayed connected and the asyncio stall
watchdog never crossed one second.  This guard addresses that real-time gap:

* add a tiny bounded playout cushion in both media directions;
* make workspace conference mode idempotent so status polling cannot continually
  reset the flow-log guard;
* cache the persisted conference toggle instead of reading SQLite on every status
  poll;
* reduce expensive full-status telemetry polling while preserving useful meters;
* suppress known informational discord-ext-voice-recv packet spam at INFO level.

The buffers are deliberately small: 60 ms PBX->Discord and 40 ms
Discord->PBX.  They trade a little latency for substantially better tolerance of
sub-second scheduler/network jitter without allowing catch-up latency to grow.
"""

import audioop
import logging
import math
from typing import Any


log = logging.getLogger("discord-pbx.media-smoothing")
_PATCHED = False
PBX_TO_DISCORD_PREBUFFER_FRAMES = 3
DISCORD_TO_PBX_PREBUFFER_FRAMES = 2


def _tune_page_polling(page: str) -> str:
    """Keep operator telemetry useful without hammering the full status route."""
    if not page:
        return page
    # Audio meters previously requested the complete /api/status payload every
    # 300 ms.  That route includes durable state/history/routing work and shares
    # the process with real-time voice.  750 ms remains visually useful while
    # cutting this source of request pressure by more than half.
    page = page.replace("window.setInterval(tick, 300);", "window.setInterval(tick, 750);")
    # Event-loop/HD state changes far more slowly than an audio meter and does not
    # need a second independent full-status request every second.
    page = page.replace("tick();setInterval(tick,1000);", "tick();setInterval(tick,2000);")
    return page


def apply() -> None:
    global _PATCHED
    if _PATCHED:
        return

    import bridge
    import media_core
    import webui

    manager_cls = bridge.BridgeManager
    session_cls = media_core.PcmMediaSession

    original_set_conference = manager_cls.set_workspace_conference_mode
    original_status = manager_cls.status_dict
    original_conf_get = webui.WebControlServer._conference_mode_get
    original_conf_set = webui.WebControlServer._conference_mode_set
    original_index = webui.WebControlServer.index

    def set_workspace_conference_mode(self, workspace_id: str, enabled: bool) -> int:
        """Do not mutate/reset diagnostics when the requested state is unchanged."""
        wid = str(workspace_id or "").strip()
        if not wid:
            raise ValueError("workspace_id is required")
        desired = bool(enabled)
        if self.workspace_conference_enabled(wid) == desired:
            return sum(
                1
                for session in self.get_sessions()
                if getattr(session, "active", False)
                and wid in list(getattr(session, "workspace_ids", []) or [])
            )
        return original_set_conference(self, wid, desired)

    def read_workspace_discord_frame(self, workspace_id: str = "") -> bytes:
        """Mix PBX audio into Discord with a 60 ms anti-jitter playout cushion."""
        silence = b"\x00" * media_core.DISCORD_FRAME_BYTES
        frames: list[bytes] = []
        wid = str(workspace_id or "")

        local = self.read_local_discord_frame(wid)
        if local and local != silence:
            frames.append(local)

        primed = getattr(self, "_pbx_to_discord_jitter_primed", None)
        if primed is None:
            primed = self._pbx_to_discord_jitter_primed = {}
            self._pbx_to_discord_underflows = 0
            self._pbx_to_discord_rebuffers = 0

        with self._workspace_audio_lock:
            calls = self._workspace_call_audio.setdefault(wid, {})
            active_ids = {
                s.call_uuid for s in self.get_sessions() if getattr(s, "active", False)
            }
            for call_uuid in list(calls):
                key = (wid, call_uuid)
                if call_uuid not in active_ids:
                    calls.pop(call_uuid, None)
                    primed.pop(key, None)
                    continue
                session = self.get_session(call_uuid)
                if not session or not getattr(session, "listen_enabled", True):
                    continue
                q = calls.get(call_uuid)
                if not q:
                    if primed.pop(key, False):
                        self._pbx_to_discord_underflows += 1
                    continue

                if not primed.get(key, False):
                    if len(q) < PBX_TO_DISCORD_PREBUFFER_FRAMES:
                        continue
                    primed[key] = True
                    self._pbx_to_discord_rebuffers += 1

                try:
                    frame = q.popleft()
                except IndexError:
                    frame = None
                    primed[key] = False
                    self._pbx_to_discord_underflows += 1
                if frame and frame != silence:
                    frames.append(frame)

        if not frames:
            return silence
        if len(frames) == 1:
            return frames[0]
        scale = 1.0 / len(frames)
        mixed = silence
        for frame in frames:
            mixed = audioop.add(mixed, audioop.mul(frame, 2, scale), 2)
        return mixed

    def _mix_next_pbx_frame(self) -> bytes:
        """Mix Discord/peer audio for Asterisk with a 40 ms anti-jitter cushion."""
        import time

        now = time.monotonic()
        target = self.tx_frame_bytes
        primed = getattr(self, "_discord_to_pbx_jitter_primed", None)
        if primed is None:
            primed = self._discord_to_pbx_jitter_primed = {}
            self._discord_to_pbx_underflows = 0
            self._discord_to_pbx_rebuffers = 0

        with self._mix_lock:
            stale = [
                uid
                for uid, st in self._users.items()
                if now - st.last_seen > 3.0 and not st.frames
            ]
            for uid in stale:
                self._users.pop(uid, None)
                primed.pop(uid, None)

            frames: list[bytes] = []
            for uid, state in self._users.items():
                if not state.frames:
                    if primed.pop(uid, False):
                        self._discord_to_pbx_underflows += 1
                    continue
                if not primed.get(uid, False):
                    if len(state.frames) < DISCORD_TO_PBX_PREBUFFER_FRAMES:
                        continue
                    primed[uid] = True
                    self._discord_to_pbx_rebuffers += 1
                try:
                    frames.append(state.frames.popleft())
                except IndexError:
                    primed[uid] = False
                    self._discord_to_pbx_underflows += 1

        if not frames:
            return b"\x00" * target
        if len(frames) == 1:
            return frames[0]
        scale = min(1.0, 1.15 / math.sqrt(len(frames)))
        mixed = b"\x00" * target
        for frame in frames:
            mixed = audioop.add(mixed, audioop.mul(frame, 2, scale), 2)
            mixed = media_core._limit_pcm(mixed, 27500)
        return mixed

    def status_dict(self) -> dict[str, Any]:
        data = original_status(self)
        sessions = [s for s in self.get_sessions() if getattr(s, "active", False)]
        data["media_jitter_guard"] = {
            "enabled": True,
            "pbx_to_discord_prebuffer_frames": PBX_TO_DISCORD_PREBUFFER_FRAMES,
            "discord_to_pbx_prebuffer_frames": DISCORD_TO_PBX_PREBUFFER_FRAMES,
            "pbx_to_discord_underflows": int(
                getattr(self, "_pbx_to_discord_underflows", 0) or 0
            ),
            "pbx_to_discord_rebuffers": int(
                getattr(self, "_pbx_to_discord_rebuffers", 0) or 0
            ),
            "discord_to_pbx_underflows": sum(
                int(getattr(s, "_discord_to_pbx_underflows", 0) or 0) for s in sessions
            ),
            "discord_to_pbx_rebuffers": sum(
                int(getattr(s, "_discord_to_pbx_rebuffers", 0) or 0) for s in sessions
            ),
            "calls": [
                {
                    "uuid": str(getattr(s, "call_uuid", "") or ""),
                    "discord_to_pbx_underflows": int(
                        getattr(s, "_discord_to_pbx_underflows", 0) or 0
                    ),
                    "discord_to_pbx_rebuffers": int(
                        getattr(s, "_discord_to_pbx_rebuffers", 0) or 0
                    ),
                }
                for s in sessions
            ],
        }
        return data

    def conference_mode_get(self, workspace_id: str) -> bool:
        wid = str(workspace_id or "").strip()
        cache = getattr(self, "_conference_mode_cache", None)
        if cache is None:
            cache = self._conference_mode_cache = {}
        if wid not in cache:
            cache[wid] = bool(original_conf_get(self, wid))
        return bool(cache[wid])

    def conference_mode_set(self, workspace_id: str, enabled: bool) -> int:
        result = original_conf_set(self, workspace_id, enabled)
        cache = getattr(self, "_conference_mode_cache", None)
        if cache is None:
            cache = self._conference_mode_cache = {}
        cache[str(workspace_id or "").strip()] = bool(enabled)
        return result

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if (
                getattr(response, "status", 200) == 200
                and "text/html" in str(getattr(response, "content_type", ""))
            ):
                response.text = _tune_page_polling(response.text)
        except Exception:
            log.exception("Could not tune operator telemetry polling")
        return response

    manager_cls.set_workspace_conference_mode = set_workspace_conference_mode
    manager_cls.read_workspace_discord_frame = read_workspace_discord_frame
    manager_cls.status_dict = status_dict
    session_cls._mix_next_pbx_frame = _mix_next_pbx_frame
    webui.WebControlServer._conference_mode_get = conference_mode_get
    webui.WebControlServer._conference_mode_set = conference_mode_set
    webui.WebControlServer.index = index

    # These are known informational compatibility messages, not call faults.
    # Keep WARNING/ERROR visible while removing synchronous Docker-log pressure
    # from one RTCP Sender Report every second and handshake key notices.
    logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
    logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.WARNING)

    manager_cls._media_smoothing_guard_applied = True
    session_cls._media_smoothing_guard_applied = True
    webui.WebControlServer._media_smoothing_guard_applied = True
    _PATCHED = True
    log.info(
        "Installed media smoothing guard (PBX->Discord %d frames, Discord->PBX %d frames; reduced telemetry polling)",
        PBX_TO_DISCORD_PREBUFFER_FRAMES,
        DISCORD_TO_PBX_PREBUFFER_FRAMES,
    )
