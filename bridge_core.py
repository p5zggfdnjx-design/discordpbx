from __future__ import annotations

import asyncio
import audioop
import logging
import math
import threading
import time
import uuid
import wave
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import voice_recv

from audiosocket import DISCORD_FRAME_BYTES

log = logging.getLogger("discord-pbx.bridge")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PBXAudioSource(discord.AudioSource):
    """Discord pulls one 20 ms mix for one configured workspace/guild."""

    def __init__(self, manager: "BridgeManager", workspace_id: str = ""):
        self.manager = manager
        self.workspace_id = str(workspace_id or "")
        self.silence = b"\x00" * DISCORD_FRAME_BYTES

    def read(self) -> bytes:
        return self.manager.read_workspace_discord_frame(self.workspace_id)

    def is_opus(self) -> bool:
        return False


class DiscordAudioSink(voice_recv.AudioSink):
    """Receives Discord PCM from one guild and routes it only to calls assigned there."""

    def __init__(self, manager: "BridgeManager", workspace_id: str = ""):
        super().__init__()
        self.manager = manager
        self.workspace_id = str(workspace_id or "")

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None or getattr(user, "bot", False):
            return
        pcm = getattr(data, "pcm", None)
        if not pcm:
            return
        self.manager.push_discord_pcm(self.workspace_id, int(user.id), pcm)

    def cleanup(self) -> None:
        pass


class BridgeManager:
    def __init__(self, bot, config):
        self.bot = bot
        self.config = config
        self._sessions: dict[str, object] = {}
        self._sessions_lock = threading.RLock()
        # One voice connection is supported per Discord guild. v3 serializes setup
        # independently per workspace so rapid calls do not race channel.connect().
        self._voice_locks: dict[str, asyncio.Lock] = {}
        self._voice_sources: dict[str, PBXAudioSource] = {}
        self._voice_sinks: dict[str, DiscordAudioSink] = {}
        self.workspace_provider = None
        self._pending: dict[str, dict] = {}
        self._leave_tasks: dict[str, asyncio.Task] = {}
        # Backwards-compatible aliases point at the legacy/default workspace source.
        self.source = PBXAudioSource(self, "")
        self.sink = DiscordAudioSink(self, "")
        # Per-workspace PBX audio queues make the same phone call audible in several
        # guilds simultaneously without one Discord source consuming another's frame.
        self._workspace_audio_lock = threading.RLock()
        self._workspace_call_audio: dict[str, dict[str, deque[bytes]]] = {}
        self.history: deque[dict] = deque(maxlen=60)
        # Optional callback installed by the web control layer for durable history,
        # audit logging, and auto-redial orchestration.
        self.event_callback = None
        self._manual_hangups: set[str] = set()
        self._pending_timeouts: deque[dict] = deque(maxlen=200)
        # Direct caller-to-caller conference routing.
        self._conference_groups: dict[str, set[str]] = {}
        # Workspace conference mode is cached here for the 20 ms audio hot path.
        # The web control layer persists the enabled state per workspace and
        # rehydrates/self-heals this cache on startup/status requests.
        self._workspace_conference_enabled: set[str] = set()
        # Conference diagnostics make the live behavior observable from the web UI.
        # Counts are intentionally runtime-only; the enabled state is persisted by webui.py.
        self._workspace_conference_frames: dict[str, int] = {}
        self._workspace_conference_last_route: dict[str, float] = {}
        self._workspace_conference_flow_logged: set[str] = set()
        # Runtime master gains controlled from the operator UI.
        self.pbx_to_discord_master_gain = 1.0
        self.discord_to_pbx_master_gain = 1.0
        self.inbound_chime_master_gain = 1.0
        # Global outbound voicemail-detection preference. WebControlServer loads
        # the persisted value at startup; Discord slash dialing inherits it too.
        self.voicemail_detection_enabled = True

        # Local Discord-only cue mixer. These frames are intentionally separate
        # from per-call talk routing so callers never hear the UI notification
        # chime or the synthetic outbound ringback.
        self._local_audio_lock = threading.RLock()
        self._alert_frames: deque[bytes] = deque()  # legacy/default workspace
        self._workspace_alert_frames: dict[str, deque[bytes]] = {}
        # call_uuid -> {deadline: float, workspaces: set[str]}
        self._ringback_calls: dict[str, dict] = {}
        self._ringback_muted = False
        self._ringback_frame_index = 0
        self._ringback_frames = self._build_ringback_cycle()
        self._inbound_chime_frames = self._load_inbound_chime()

    # ---------- local Discord-only cues ----------
    @staticmethod
    def _split_discord_frames(pcm: bytes) -> list[bytes]:
        frames: list[bytes] = []
        silence = b"\x00" * DISCORD_FRAME_BYTES
        for pos in range(0, len(pcm), DISCORD_FRAME_BYTES):
            frame = pcm[pos:pos + DISCORD_FRAME_BYTES]
            if len(frame) < DISCORD_FRAME_BYTES:
                frame += silence[:DISCORD_FRAME_BYTES - len(frame)]
            frames.append(frame)
        return frames

    def _load_inbound_chime(self) -> list[bytes]:
        if not self.config.inbound_chime_enabled:
            return []
        path = self.config.inbound_chime_file
        try:
            with wave.open(path, "rb") as wav:
                if wav.getnchannels() != 2 or wav.getsampwidth() != 2 or wav.getframerate() != 48000:
                    raise ValueError("inbound chime WAV must be 48 kHz, 16-bit, stereo PCM")
                pcm = wav.readframes(wav.getnframes())
            if self.config.inbound_chime_gain != 1.0:
                pcm = audioop.mul(pcm, 2, self.config.inbound_chime_gain)
            frames = self._split_discord_frames(pcm)
            log.info("Loaded inbound notification chime %s (%d frames)", path, len(frames))
            return frames
        except Exception as exc:
            log.warning("Inbound notification chime unavailable (%s): %s", path, exc)
            return []

    @staticmethod
    def _tone_frame(start_sample: int, samples: int, gain: float = 0.13) -> bytes:
        """Generate one stereo 48 kHz frame of US-style 440+480 Hz ringback."""
        out = bytearray(samples * 4)
        for i in range(samples):
            t = (start_sample + i) / 48000.0
            value = int(32767 * gain * 0.5 * (math.sin(2 * math.pi * 440 * t) + math.sin(2 * math.pi * 480 * t)))
            value = max(-32768, min(32767, value))
            b = int(value).to_bytes(2, "little", signed=True)
            off = i * 4
            out[off:off + 2] = b
            out[off + 2:off + 4] = b
        return bytes(out)

    def _build_ringback_cycle(self) -> list[bytes]:
        # North-American ringback cadence: 2 seconds on, 4 seconds off.
        frame_samples = 960  # 20 ms at 48 kHz
        total_frames = 300
        on_frames = 100
        silence = b"\x00" * DISCORD_FRAME_BYTES
        frames: list[bytes] = []
        for idx in range(total_frames):
            if idx < on_frames:
                frames.append(self._tone_frame(idx * frame_samples, frame_samples))
            else:
                frames.append(silence)
        return frames

    def _normalize_workspace_ids(self, workspace_ids=None) -> list[str]:
        if workspace_ids is not None:
            out = [str(x) for x in workspace_ids if str(x)]
            return list(dict.fromkeys(out))
        if self.workspace_provider is not None:
            try:
                ws = self.workspace_provider.default_workspace()
                if ws:
                    return [str(ws["id"])]
            except Exception:
                pass
        return [""]

    def queue_inbound_chime(self, workspace_ids=None) -> None:
        if not self._inbound_chime_frames or self.inbound_chime_master_gain <= 0:
            return
        frames = self._inbound_chime_frames
        if self.inbound_chime_master_gain != 1.0:
            frames = [audioop.mul(frame, 2, self.inbound_chime_master_gain) for frame in frames]
        with self._local_audio_lock:
            for wid in self._normalize_workspace_ids(workspace_ids):
                if wid:
                    q = self._workspace_alert_frames.setdefault(wid, deque())
                    q.extend(frames)
                else:
                    self._alert_frames.extend(frames)

    @property
    def ringback_muted(self) -> bool:
        with self._local_audio_lock:
            return bool(self._ringback_muted)

    def set_ringback_muted(self, muted: bool) -> None:
        with self._local_audio_lock:
            self._ringback_muted = bool(muted)
            if muted:
                self._ringback_frame_index = 0

    def start_outbound_ringback(self, call_uuid: str, workspace_ids=None) -> None:
        deadline = time.monotonic() + max(5.0, self.config.ami_dial_timeout_ms / 1000.0 + 3.0)
        with self._local_audio_lock:
            self._ringback_calls[call_uuid] = {
                "deadline": deadline,
                "workspaces": set(self._normalize_workspace_ids(workspace_ids)),
            }

    def stop_outbound_ringback(self, call_uuid: str) -> None:
        with self._local_audio_lock:
            self._ringback_calls.pop(call_uuid, None)
            if not self._ringback_calls:
                self._ringback_frame_index = 0

    def read_local_discord_frame(self, workspace_id: str = "") -> bytes:
        frames: list[bytes] = []
        silence = b"\x00" * DISCORD_FRAME_BYTES
        workspace_id = str(workspace_id or "")
        with self._local_audio_lock:
            now = time.monotonic()
            expired = [uid for uid, info in self._ringback_calls.items() if float(info.get("deadline", 0)) <= now]
            for uid in expired:
                self._ringback_calls.pop(uid, None)

            if workspace_id:
                q = self._workspace_alert_frames.get(workspace_id)
                if q:
                    frames.append(q.popleft())
                    if not q:
                        self._workspace_alert_frames.pop(workspace_id, None)
            elif self._alert_frames:
                frames.append(self._alert_frames.popleft())

            ringback_here = any(
                (workspace_id in info.get("workspaces", set())) if workspace_id else ("" in info.get("workspaces", set()))
                for info in self._ringback_calls.values()
            )
            if ringback_here and self._ringback_frames and not self._ringback_muted:
                frames.append(self._ringback_frames[self._ringback_frame_index])
                self._ringback_frame_index = (self._ringback_frame_index + 1) % len(self._ringback_frames)
            elif not self._ringback_calls or self._ringback_muted:
                self._ringback_frame_index = 0

        audible = [f for f in frames if f and f != silence]
        if not audible:
            return silence
        mixed = silence
        for frame in audible:
            mixed = audioop.add(mixed, frame, 2)
        return mixed

    # ---------- per-workspace audio fanout ----------
    def publish_pbx_frame(self, session, frame: bytes) -> None:
        if not frame or not getattr(session, "active", False) or not getattr(session, "listen_enabled", True):
            return
        workspace_ids = self._normalize_workspace_ids(getattr(session, "workspace_ids", []))
        with self._workspace_audio_lock:
            for wid in workspace_ids:
                calls = self._workspace_call_audio.setdefault(wid, {})
                q = calls.get(session.call_uuid)
                if q is None:
                    q = deque(maxlen=10)
                    calls[session.call_uuid] = q
                q.append(frame)

    def read_workspace_discord_frame(self, workspace_id: str = "") -> bytes:
        silence = b"\x00" * DISCORD_FRAME_BYTES
        frames: list[bytes] = []
        local = self.read_local_discord_frame(workspace_id)
        if local and local != silence:
            frames.append(local)
        with self._workspace_audio_lock:
            calls = self._workspace_call_audio.setdefault(str(workspace_id or ""), {})
            active_ids = {s.call_uuid for s in self.get_sessions() if getattr(s, "active", False)}
            for call_uuid in list(calls):
                if call_uuid not in active_ids:
                    calls.pop(call_uuid, None)
                    continue
                session = self.get_session(call_uuid)
                if not session or not getattr(session, "listen_enabled", True):
                    continue
                q = calls.get(call_uuid)
                if q:
                    try:
                        frame = q.popleft()
                    except IndexError:
                        frame = None
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

    # ---------- session registry ----------
    def get_sessions(self) -> list:
        with self._sessions_lock:
            return list(self._sessions.values())

    def get_session(self, call_uuid: str):
        with self._sessions_lock:
            return self._sessions.get(call_uuid)

    @property
    def active_session(self):
        """Backwards-compatible helper: return first active session, if any."""
        sessions = self.get_sessions()
        return sessions[0] if sessions else None

    def prepare_outbound(self, number: str, caller_id: str = "", contact_name: str = "", source: str = "manual", randomize_caller_id: bool = False, retry_of: str = "", retry_index: int = 0, voicemail_detection_enabled: Optional[bool] = None, workspace_ids=None, operator_user_id: str = "", operator_name: str = "") -> str:
        call_uuid = str(uuid.uuid4())
        now = time.time()
        workspace_ids = self._normalize_workspace_ids(workspace_ids)
        with self._sessions_lock:
            self._pending[call_uuid] = {
                "direction": "outbound",
                "number": number,
                "caller_id": caller_id,
                "contact_name": contact_name,
                "time": utc_now(),
                "created_ts": now,
                "deadline_ts": now + max(5.0, self.config.ami_dial_timeout_ms / 1000.0 + 5.0),
                "state": "dialing / ringing",
                "source": str(source or "manual"),
                "randomize_caller_id": bool(randomize_caller_id),
                "retry_of": str(retry_of or ""),
                "retry_index": int(retry_index or 0),
                "voicemail_detection_enabled": bool(self.voicemail_detection_enabled if voicemail_detection_enabled is None else voicemail_detection_enabled),
                "workspace_ids": workspace_ids,
                "operator_user_id": str(operator_user_id or ""),
                "operator_name": str(operator_name or ""),
            }
        self.start_outbound_ringback(call_uuid, workspace_ids)
        return call_uuid

    def prepare_inbound(self, call_uuid: str, number: str = "", contact_name: str = "", workspace_ids=None) -> None:
        """Register metadata before Asterisk connects its AudioSocket UUID."""
        with self._sessions_lock:
            self._pending[call_uuid] = {
                "direction": "inbound",
                "number": number,
                "caller_id": "",
                "contact_name": contact_name,
                "time": utc_now(),
                "source": "inbound",
                "workspace_ids": self._normalize_workspace_ids(workspace_ids),
            }

    def cancel_pending(self, call_uuid: str) -> None:
        with self._sessions_lock:
            item = self._pending.pop(call_uuid, None)
        self.stop_outbound_ringback(call_uuid)
        for wid in list((item or {}).get("workspace_ids", []) or []):
            self.schedule_voice_idle_disconnect(wid)

    def get_pending(self, call_uuid: str) -> dict | None:
        with self._sessions_lock:
            item = self._pending.get(call_uuid)
            return dict(item) if item else None

    def update_pending_state(self, call_uuid: str, state: str) -> bool:
        """Update the operator-visible state for a pending outbound call."""
        with self._sessions_lock:
            item = self._pending.get(call_uuid)
            if not item or item.get("direction") != "outbound":
                return False
            item["state"] = str(state or "starting")[:120]
            return True

    def fail_pending(self, call_uuid: str, detail: str = "") -> None:
        """Remove a failed originate and leave a concise event in Activity."""
        with self._sessions_lock:
            item = self._pending.pop(call_uuid, None)
        self.stop_outbound_ringback(call_uuid)
        if not item:
            return
        for wid in list(item.get("workspace_ids", []) or []):
            self.schedule_voice_idle_disconnect(wid)
        self.history.appendleft({
            "event": "dial failed",
            "uuid": call_uuid,
            "direction": "outbound",
            "number": item.get("number", ""),
            "caller_id": item.get("caller_id", ""),
            "contact_name": item.get("contact_name", ""),
            "detail": str(detail or "Outbound originate failed")[:240],
            "time": utc_now(),
        })

    def _prune_stale_outbound_pending(self) -> None:
        now = time.time()
        stale: list[str] = []
        with self._sessions_lock:
            for uid, item in self._pending.items():
                if item.get("direction") != "outbound":
                    continue
                deadline = float(item.get("deadline_ts", 0) or 0)
                if deadline and deadline <= now:
                    stale.append(uid)
            timed_out = []
            for uid in stale:
                item = self._pending.pop(uid, None)
                if item:
                    timed_out.append({"uuid": uid, **dict(item)})
        for item in timed_out:
            uid = item.get("uuid", "")
            self.stop_outbound_ringback(uid)
            for wid in list(item.get("workspace_ids", []) or []):
                self.schedule_voice_idle_disconnect(wid)
            item["outcome"] = "no answer"
            item["detail"] = "Outbound call timed out without an AudioSocket answer"
            self._pending_timeouts.append(item)
            self.history.appendleft({
                "event": "no answer", "uuid": uid, "direction": "outbound",
                "number": item.get("number", ""), "caller_id": item.get("caller_id", ""),
                "contact_name": item.get("contact_name", ""), "detail": item["detail"], "time": utc_now(),
            })

    def outbound_pending(self) -> list[dict]:
        self._prune_stale_outbound_pending()
        now = time.time()
        out: list[dict] = []
        with self._sessions_lock:
            for uid, item in self._pending.items():
                if item.get("direction") != "outbound":
                    continue
                created = float(item.get("created_ts", now) or now)
                out.append({
                    "uuid": uid,
                    "short_uuid": uid[:8],
                    "number": item.get("number", ""),
                    "caller_id": item.get("caller_id", ""),
                    "contact_name": item.get("contact_name", ""),
                    "state": item.get("state", "dialing / ringing"),
                    "age_seconds": round(max(0.0, now - created), 1),
                    "source": item.get("source", "manual"),
                    "randomize_caller_id": bool(item.get("randomize_caller_id", False)),
                    "retry_of": item.get("retry_of", ""),
                    "retry_index": int(item.get("retry_index", 0) or 0),
                    "voicemail_detection_enabled": bool(item.get("voicemail_detection_enabled", False)),
                    "workspace_ids": list(item.get("workspace_ids", [])),
                    "operator_user_id": item.get("operator_user_id", ""),
                    "operator_name": item.get("operator_name", ""),
                })
        return sorted(out, key=lambda x: x.get("age_seconds", 0), reverse=True)

    def _workspace_voice_config(self, workspace_id: str = "") -> tuple[str, int, int, int]:
        """Return workspace_id, guild_id, voice_channel_id, text_channel_id."""
        workspace_id = str(workspace_id or "")
        if self.workspace_provider is not None:
            ws = self.workspace_provider.db.get_workspace(workspace_id) if workspace_id else self.workspace_provider.default_workspace()
            if ws:
                return str(ws["id"]), int(ws["guild_id"]), int(ws.get("voice_channel_id") or 0), int(ws.get("text_channel_id") or 0)
        return workspace_id, int(self.config.guild_id or 0), int(self.config.voice_channel_id or 0), int(self.config.text_channel_id or 0)

    def _workspace_has_voice_work(self, workspace_id: str) -> bool:
        """Return True while a workspace has an active or pending PBX call."""
        wid = str(workspace_id or "")
        if not wid:
            return False
        with self._sessions_lock:
            for session in self._sessions.values():
                if wid in list(getattr(session, "workspace_ids", []) or []):
                    return True
            for item in self._pending.values():
                if wid in list(item.get("workspace_ids", []) or []):
                    return True
        return False

    def _cancel_voice_leave(self, workspace_id: str) -> None:
        wid = str(workspace_id or "")
        task = self._leave_tasks.pop(wid, None)
        if task and not task.done():
            task.cancel()

    def schedule_voice_idle_disconnect(self, workspace_id: str, delay: float | None = None) -> None:
        """Disconnect one guild's voice connection after its own calls are idle."""
        wid = str(workspace_id or "")
        if not wid:
            return
        self._cancel_voice_leave(wid)
        if self._workspace_has_voice_work(wid):
            return
        seconds = self.config.leave_voice_after_call_seconds if delay is None else float(delay)
        if seconds < 0:
            # v3.2 intentionally does not support permanent idle voice residency.
            seconds = 15.0
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._leave_tasks[wid] = loop.create_task(
            self._leave_workspace_after(wid, seconds),
            name=f"discord-voice-idle-{wid[:24]}",
        )

    async def _leave_workspace_after(self, workspace_id: str, delay: float) -> None:
        current = asyncio.current_task()
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            if not self._workspace_has_voice_work(workspace_id):
                await self.disconnect_voice(workspace_id, _from_idle_task=True)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Failed leaving idle Discord voice for workspace %s", workspace_id)
        finally:
            if self._leave_tasks.get(workspace_id) is current:
                self._leave_tasks.pop(workspace_id, None)

    async def ensure_voice(self, workspace_id: str = ""):
        workspace_id, guild_id, voice_channel_id, _ = self._workspace_voice_config(workspace_id)
        self._cancel_voice_leave(workspace_id)
        if not guild_id or not voice_channel_id:
            raise RuntimeError("Discord workspace has no configured voice channel")
        lock = self._voice_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            await self.bot.wait_until_ready()
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                raise RuntimeError(f"Discord bot is not connected to guild {guild_id}")
            channel = guild.get_channel(voice_channel_id) or self.bot.get_channel(voice_channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(voice_channel_id)
                except Exception as exc:
                    raise RuntimeError(f"Could not fetch Discord voice channel {voice_channel_id}: {exc}") from exc
            if not isinstance(channel, discord.VoiceChannel):
                raise RuntimeError("Configured Discord voice channel must be a normal voice channel")

            vc = guild.voice_client
            if vc is not None and not isinstance(vc, voice_recv.VoiceRecvClient):
                await vc.disconnect(force=True)
                vc = None
            if vc is None:
                log.info("Joining Discord workspace %s voice channel %s", workspace_id or guild_id, channel)
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False, self_mute=False)
            elif vc.channel.id != channel.id:
                await vc.move_to(channel)

            source = self._voice_sources.setdefault(workspace_id, PBXAudioSource(self, workspace_id))
            sink = self._voice_sinks.setdefault(workspace_id, DiscordAudioSink(self, workspace_id))
            if not vc.is_playing():
                vc.play(source, after=self._playback_after)
            if not vc.is_listening():
                vc.listen(sink, after=self._listen_after)
            return vc

    def _playback_after(self, error: Optional[Exception]) -> None:
        if error:
            log.error("Discord playback stopped: %s", error)
        else:
            log.warning("Discord playback stopped unexpectedly")

    def _listen_after(self, error: Optional[Exception]) -> None:
        if error:
            log.error("Discord receive stopped: %s", error)
        else:
            log.warning("Discord receive stopped unexpectedly")

    async def disconnect_voice(self, workspace_id: str | None = None, _from_idle_task: bool = False) -> None:
        targets: list[tuple[str, object]] = []
        if workspace_id is not None and not _from_idle_task:
            self._cancel_voice_leave(str(workspace_id))
        elif workspace_id is None:
            for wid, task in list(self._leave_tasks.items()):
                if task is not asyncio.current_task() and not task.done():
                    task.cancel()
                self._leave_tasks.pop(wid, None)
        if workspace_id is not None:
            wid, guild_id, _, _ = self._workspace_voice_config(workspace_id)
            guild = self.bot.get_guild(guild_id) if guild_id else None
            if guild and guild.voice_client:
                targets.append((wid, guild.voice_client))
        else:
            for vc in list(getattr(self.bot, "voice_clients", [])):
                guild = getattr(vc, "guild", None)
                ws = self.workspace_provider.workspace_for_guild(guild.id) if self.workspace_provider and guild else None
                targets.append((str(ws["id"]) if ws else "", vc))
        for wid, vc in targets:
            try:
                if isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_listening():
                    vc.stop_listening()
            except Exception:
                pass
            try:
                if vc.is_playing():
                    vc.stop_playing() if hasattr(vc, "stop_playing") else vc.stop()
            except Exception:
                pass
            try:
                await vc.disconnect(force=True)
            except Exception:
                log.exception("Could not disconnect Discord workspace %s", wid)

    async def call_started(self, session) -> bool:
        call_uuid = session.call_uuid or "unknown"

        with self._sessions_lock:
            active_count = sum(1 for s in self._sessions.values() if getattr(s, "active", False))
            if active_count >= self.config.max_simultaneous_calls:
                return False

            meta = self._pending.pop(call_uuid, None) or {}
            session.direction = meta.get("direction", "inbound")
            session.remote_number = meta.get("number", "")
            session.caller_id = meta.get("caller_id", "")
            session.contact_name = meta.get("contact_name", "")
            session.source = meta.get("source", "inbound" if session.direction == "inbound" else "manual")
            session.randomize_caller_id = bool(meta.get("randomize_caller_id", False))
            session.retry_of = meta.get("retry_of", "")
            session.retry_index = int(meta.get("retry_index", 0) or 0)
            session.voicemail_detection_enabled = bool(meta.get("voicemail_detection_enabled", False))
            session.workspace_ids = list(meta.get("workspace_ids", self._normalize_workspace_ids(None)))
            session.operator_user_id = str(meta.get("operator_user_id", ""))
            session.operator_name = str(meta.get("operator_name", ""))
            session.held = False
            session.park_slot = 0
            if session.direction == "outbound" and session.voicemail_detection_enabled:
                session.enable_voicemail_detection()
            self._sessions[call_uuid] = session

            for wid in session.workspace_ids:
                self._cancel_voice_leave(wid)

        self.stop_outbound_ringback(call_uuid)

        self.history.appendleft({
            "event": "connected",
            "uuid": call_uuid,
            "direction": session.direction,
            "number": session.remote_number,
            "caller_id": session.caller_id,
            "contact_name": getattr(session, "contact_name", ""),
            "source": getattr(session, "source", ""),
            "time": utc_now(),
        })

        await self._emit_event("connected", {
            "uuid": call_uuid, "direction": session.direction, "number": session.remote_number,
            "caller_id": session.caller_id, "contact_name": getattr(session, "contact_name", ""),
            "source": getattr(session, "source", ""), "retry_of": getattr(session, "retry_of", ""),
            "retry_index": getattr(session, "retry_index", 0),
            "voicemail_detection_enabled": bool(getattr(session, "voicemail_detection_enabled", False)),
            "workspace_ids": list(getattr(session, "workspace_ids", [])),
            "operator_user_id": getattr(session, "operator_user_id", ""),
            "operator_name": getattr(session, "operator_name", ""),
        })

        workspace_ids = list(getattr(session, "workspace_ids", []))
        if not workspace_ids:
            log.warning("PBX call %s has no Discord workspace route", call_uuid)
            with self._sessions_lock:
                self._sessions.pop(call_uuid, None)
            return False
        try:
            results = await asyncio.gather(*(self.ensure_voice(wid) for wid in workspace_ids), return_exceptions=True)
            failures = [r for r in results if isinstance(r, Exception)]
            if len(failures) == len(results):
                raise RuntimeError("; ".join(str(x) for x in failures)[:500])
        except Exception:
            log.exception("Could not join any routed Discord workspace for PBX call")
            with self._sessions_lock:
                self._sessions.pop(call_uuid, None)
            return False

        if session.direction == "inbound":
            # Give a freshly joined Discord voice client a moment to begin pulling
            # PCM frames before we enqueue the notification. This fixes a startup
            # race where the first chime could be consumed before Discord playback
            # was fully established.
            async def _late_chime():
                await asyncio.sleep(0.35)
                if session.active and self.get_session(call_uuid) is session:
                    self.queue_inbound_chime(workspace_ids)
            asyncio.create_task(_late_chime(), name=f"inbound-chime-{call_uuid[:8]}")

        label = session.remote_number or call_uuid[:8]
        await self._notify(f"☎️ {session.direction.title()} PBX call connected: `{label}`", workspace_ids)
        return True

    async def voicemail_classified(self, session, result: str, cause: str = "") -> None:
        """Handle the built-in AMD-like classifier result for one outbound call."""
        call_uuid = session.call_uuid or "unknown"
        result = str(result or "NOTSURE").upper()
        cause = str(cause or "")[:120]
        payload = {
            "uuid": call_uuid, "result": result, "cause": cause,
            "number": getattr(session, "remote_number", ""),
            "caller_id": getattr(session, "caller_id", ""),
            "contact_name": getattr(session, "contact_name", ""),
            "source": getattr(session, "source", ""),
            "workspace_ids": list(getattr(session, "workspace_ids", [])),
            "operator_user_id": str(getattr(session, "operator_user_id", "")),
            "operator_name": str(getattr(session, "operator_name", "")),
        }
        if result == "MACHINE":
            session.voicemail_hangup = True
            self.history.appendleft({
                "event": "voicemail detected", "uuid": call_uuid, "direction": "outbound",
                "number": payload["number"], "caller_id": payload["caller_id"],
                "contact_name": payload["contact_name"], "source": payload["source"],
                "detail": cause, "time": utc_now(),
            })
        await self._emit_event("voicemail_result", payload)
        if result == "MACHINE":
            # End the PBX leg first; Discord/text notification must never delay
            # the actual voicemail hangup.
            await session.close()
            await self._notify(f"📼 Voicemail detected on `{payload['number'] or call_uuid[:8]}` — hung up", getattr(session, "workspace_ids", []))

    async def call_ended(self, session) -> None:
        call_uuid = session.call_uuid or "unknown"
        removed = False
        with self._sessions_lock:
            if self._sessions.get(call_uuid) is session:
                self._sessions.pop(call_uuid, None)
                removed = True

        if not removed:
            return

        self._remove_from_conference(call_uuid)
        manual = call_uuid in self._manual_hangups
        self._manual_hangups.discard(call_uuid)

        self.history.appendleft({
            "event": "ended",
            "uuid": call_uuid,
            "direction": getattr(session, "direction", "unknown"),
            "number": getattr(session, "remote_number", ""),
            "caller_id": getattr(session, "caller_id", ""),
            "contact_name": getattr(session, "contact_name", ""),
            "source": getattr(session, "source", ""),
            "time": utc_now(),
            "seconds": round(session.age_seconds, 1),
            "pbx_to_discord_bytes": session.rx_audio_bytes,
            "discord_to_pbx_bytes": session.tx_audio_bytes,
            "voicemail": bool(getattr(session, "voicemail_hangup", False)),
            "voicemail_result": getattr(session, "voicemail_detection_result", ""),
        })

        await self._emit_event("ended", {
            "uuid": call_uuid, "direction": getattr(session, "direction", "unknown"),
            "number": getattr(session, "remote_number", ""), "caller_id": getattr(session, "caller_id", ""),
            "contact_name": getattr(session, "contact_name", ""), "source": getattr(session, "source", ""),
            "seconds": round(session.age_seconds, 1), "manual": manual,
            "randomize_caller_id": bool(getattr(session, "randomize_caller_id", False)),
            "retry_of": getattr(session, "retry_of", ""), "retry_index": getattr(session, "retry_index", 0),
            "voicemail": bool(getattr(session, "voicemail_hangup", False)),
            "voicemail_result": getattr(session, "voicemail_detection_result", ""),
            "voicemail_cause": getattr(session, "voicemail_detection_cause", ""),
            "workspace_ids": list(getattr(session, "workspace_ids", [])),
            "operator_user_id": str(getattr(session, "operator_user_id", "")),
            "operator_name": str(getattr(session, "operator_name", "")),
        })

        await self._notify(
            f"☎️ PBX call ended (`{call_uuid[:8]}`) — "
            f"PBX→Discord {session.rx_audio_bytes / 1024:.1f} KiB, "
            f"Discord→PBX {session.tx_audio_bytes / 1024:.1f} KiB",
            list(getattr(session, "workspace_ids", [])),
        )

        # Disconnect only the guild(s) that became idle. Other guilds may still
        # have simultaneous calls and keep their own independent voice clients.
        for wid in list(getattr(session, "workspace_ids", [])):
            self.schedule_voice_idle_disconnect(wid)

    async def dtmf_received(self, session, digit: str) -> None:
        log.info("DTMF from PBX call %s: %s", session.call_uuid, digit)

    async def _emit_event(self, event: str, payload: dict) -> None:
        cb = self.event_callback
        if not cb:
            return
        try:
            result = cb(event, dict(payload))
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("Bridge event callback failed: %s", event)

    def drain_pending_timeouts(self) -> list[dict]:
        items: list[dict] = []
        while True:
            try:
                items.append(self._pending_timeouts.popleft())
            except IndexError:
                break
        return items

    def set_master_gains(self, *, caller_to_discord: Optional[float] = None, discord_to_caller: Optional[float] = None, inbound_chime: Optional[float] = None) -> None:
        if caller_to_discord is not None:
            self.pbx_to_discord_master_gain = max(0.0, min(2.0, float(caller_to_discord)))
        if discord_to_caller is not None:
            self.discord_to_pbx_master_gain = max(0.0, min(2.0, float(discord_to_caller)))
        if inbound_chime is not None:
            self.inbound_chime_master_gain = max(0.0, min(2.0, float(inbound_chime)))

    def set_all_listen(self, enabled: bool) -> int:
        count = 0
        for session in self.get_sessions():
            if session.active:
                session.listen_enabled = bool(enabled)
                count += 1
        return count

    def create_conference(self, call_uuids: list[str]) -> tuple[bool, str, str]:
        unique = []
        for uid in call_uuids:
            if uid not in unique:
                unique.append(uid)
        sessions = [self.get_session(uid) for uid in unique]
        sessions = [s for s in sessions if s and s.active]
        if len(sessions) < 2:
            return False, "Select at least two active calls to bridge.", ""
        for session in sessions:
            self._remove_from_conference(session.call_uuid)
        gid = str(uuid.uuid4())
        members = {s.call_uuid for s in sessions}
        self._conference_groups[gid] = members
        for session in sessions:
            session.conference_group = gid
        return True, f"Bridged {len(members)} calls into one conference.", gid

    def _remove_from_conference(self, call_uuid: str) -> bool:
        removed = False
        for gid, members in list(self._conference_groups.items()):
            if call_uuid not in members:
                continue
            members.discard(call_uuid)
            removed = True
            session = self.get_session(call_uuid)
            if session:
                session.conference_group = ""
            if len(members) < 2:
                for uid in list(members):
                    peer = self.get_session(uid)
                    if peer:
                        peer.conference_group = ""
                self._conference_groups.pop(gid, None)
            else:
                self._conference_groups[gid] = members
        return removed

    def split_call(self, call_uuid: str) -> bool:
        return self._remove_from_conference(call_uuid)

    def workspace_conference_enabled(self, workspace_id: str) -> bool:
        return str(workspace_id or "") in self._workspace_conference_enabled

    def conference_diagnostics(self, workspace_id: str) -> dict:
        wid = str(workspace_id or "").strip()
        eligible = [
            session for session in self.get_sessions()
            if getattr(session, "active", False)
            and not getattr(session, "held", False)
            and wid in list(getattr(session, "workspace_ids", []) or [])
        ]
        last = float(self._workspace_conference_last_route.get(wid, 0.0) or 0.0)
        return {
            "enabled": self.workspace_conference_enabled(wid),
            "eligible_calls": len(eligible),
            "routed_frames": int(self._workspace_conference_frames.get(wid, 0) or 0),
            "peer_frames_received": sum(int(getattr(s, "conference_peer_frames_rx", 0) or 0) for s in eligible),
            "peer_sources": sum(len(getattr(s, "conference_peer_sources", set()) or set()) for s in eligible),
            "last_routed_seconds_ago": (round(max(0.0, time.monotonic() - last), 2) if last else None),
        }

    def set_workspace_conference_mode(self, workspace_id: str, enabled: bool) -> int:
        """Enable/disable automatic caller-to-caller audio for one workspace.

        The mode is evaluated dynamically by ``push_conference_pcm`` instead of
        assigning a call to a single conference group. That lets a call routed to
        multiple Discord workspaces participate correctly if more than one of
        those workspaces has conference mode enabled.
        """
        wid = str(workspace_id or "").strip()
        if not wid:
            raise ValueError("workspace_id is required")
        if enabled:
            self._workspace_conference_enabled.add(wid)
            self._workspace_conference_flow_logged.discard(wid)
        else:
            self._workspace_conference_enabled.discard(wid)
            self._workspace_conference_flow_logged.discard(wid)
        return sum(
            1
            for session in self.get_sessions()
            if session.active and wid in list(getattr(session, "workspace_ids", []) or [])
        )

    def push_conference_pcm(self, source_uuid: str, pcm_48k_stereo: bytes) -> int:
        source = self.get_session(source_uuid)
        if not source or not source.active or getattr(source, "held", False):
            return 0
        # "Mute Caller" should also keep that caller out of the caller-to-caller
        # conference mix. This matches the operator's expectation that muting a
        # caller stops their outbound audio everywhere.
        if not bool(getattr(source, "listen_enabled", True)):
            return 0

        targets: set[str] = set()

        # Existing explicit/manual conference groups still work.
        gid = getattr(source, "conference_group", "")
        if gid and gid in self._conference_groups:
            targets.update(self._conference_groups.get(gid, set()))

        # Workspace conference mode is additive and supports calls routed to
        # multiple workspaces without forcing a single conference_group value.
        source_workspaces = set(str(x) for x in (getattr(source, "workspace_ids", []) or []))
        conference_workspaces = source_workspaces.intersection(self._workspace_conference_enabled)
        if conference_workspaces:
            for peer in self.get_sessions():
                if not peer.active or peer.call_uuid == source_uuid or getattr(peer, "held", False):
                    continue
                peer_workspaces = set(str(x) for x in (getattr(peer, "workspace_ids", []) or []))
                if conference_workspaces.intersection(peer_workspaces):
                    targets.add(peer.call_uuid)

        targets.discard(source_uuid)
        if not targets:
            return 0

        count = 0
        # Keep one stable synthetic mixer source per remote caller. This means a
        # target session can mix Discord speakers and every other caller without
        # treating conference audio as Discord input or feeding it back to source.
        pseudo_id = -abs(hash(("conference", source_uuid))) or -1
        for uid in list(targets):
            peer = self.get_session(uid)
            if peer and peer.active and not getattr(peer, "held", False):
                queued = peer.push_peer_pcm(pseudo_id, pcm_48k_stereo)
                if queued:
                    count += 1

        if count and conference_workspaces:
            now = time.monotonic()
            for wid in conference_workspaces:
                self._workspace_conference_frames[wid] = int(self._workspace_conference_frames.get(wid, 0) or 0) + count
                self._workspace_conference_last_route[wid] = now
                if wid not in self._workspace_conference_flow_logged:
                    self._workspace_conference_flow_logged.add(wid)
                    log.info("Conference caller audio is flowing for workspace %s (%d peer route%s)", wid, count, "" if count == 1 else "s")
        return count

    # ---------- audio routing ----------
    def push_discord_pcm(self, workspace_or_user, user_or_pcm, pcm: bytes | None = None) -> None:
        # v2 compatibility: push_discord_pcm(user_id, pcm) remains a global feed
        # used by Discord soundboard forwarding. Workspace sinks use the 3-arg form.
        if pcm is None:
            workspace_id = ""
            user_id = int(workspace_or_user)
            pcm = user_or_pcm
        else:
            workspace_id = str(workspace_or_user or "")
            user_id = int(user_or_pcm)
        for session in self.get_sessions():
            if not session.active or not session.talk_enabled or getattr(session, "held", False):
                continue
            routed = list(getattr(session, "workspace_ids", []))
            if workspace_id and workspace_id not in routed:
                continue
            session.push_discord_pcm(user_id, pcm)

    def set_call_workspaces(self, call_uuid: str, workspace_ids) -> bool:
        session = self.get_session(call_uuid)
        if not session or not session.active:
            return False
        new_ids = self._normalize_workspace_ids(workspace_ids)
        session.workspace_ids = new_ids
        return True

    async def add_call_workspace(self, call_uuid: str, workspace_id: str) -> bool:
        session = self.get_session(call_uuid)
        if not session or not session.active:
            return False
        ids = list(getattr(session, "workspace_ids", []))
        if workspace_id not in ids:
            ids.append(workspace_id)
        await self.ensure_voice(workspace_id)
        self._cancel_voice_leave(workspace_id)
        session.workspace_ids = ids
        return True

    def remove_call_workspace(self, call_uuid: str, workspace_id: str) -> bool:
        session = self.get_session(call_uuid)
        if not session or not session.active:
            return False
        ids = [x for x in getattr(session, "workspace_ids", []) if x != workspace_id]
        session.workspace_ids = ids
        self.schedule_voice_idle_disconnect(workspace_id)
        return True

    def set_hold(self, call_uuid: str, held: bool) -> bool:
        session = self.get_session(call_uuid)
        if not session or not session.active:
            return False
        held = bool(held)
        if held and not getattr(session, "held", False):
            session._prehold_routes = (bool(session.listen_enabled), bool(session.talk_enabled))
            session.listen_enabled = False
            session.talk_enabled = False
            session.held = True
        elif not held and getattr(session, "held", False):
            listen, talk = getattr(session, "_prehold_routes", (True, True))
            session.listen_enabled, session.talk_enabled = listen, talk
            session.held = False
        return True

    def push_web_sound_pcm(self, user_id: int, pcm: bytes, call_uuid: str = "") -> int:
        """Feed a web soundboard frame to all talk-enabled calls, or one selected call."""
        if call_uuid:
            session = self.get_session(call_uuid)
            if not session or not session.active or not session.talk_enabled:
                return 0
            session.push_discord_pcm(user_id, pcm)
            return 1

        count = 0
        for session in self.get_sessions():
            if session.active and session.talk_enabled:
                session.push_discord_pcm(user_id, pcm)
                count += 1
        return count

    def set_call_routes(
        self,
        call_uuid: str,
        *,
        listen_enabled: Optional[bool] = None,
        talk_enabled: Optional[bool] = None,
    ) -> bool:
        session = self.get_session(call_uuid)
        if not session or not session.active:
            return False
        if listen_enabled is not None:
            session.listen_enabled = bool(listen_enabled)
        if talk_enabled is not None:
            session.talk_enabled = bool(talk_enabled)
        return True

    def solo_talk(self, call_uuid: str) -> bool:
        target = self.get_session(call_uuid)
        if not target or not target.active:
            return False
        for session in self.get_sessions():
            session.talk_enabled = session is target
        return True

    def focus_call(self, call_uuid: str) -> bool:
        """Private focus: only the selected call can hear Discord and be heard in Discord."""
        target = self.get_session(call_uuid)
        if not target or not target.active:
            return False
        for session in self.get_sessions():
            enabled = session is target
            session.talk_enabled = enabled
            session.listen_enabled = enabled
        return True

    def set_all_talk(self, enabled: bool) -> int:
        count = 0
        for session in self.get_sessions():
            if session.active:
                session.talk_enabled = bool(enabled)
                count += 1
        return count

    def enable_all_routes(self) -> None:
        for session in self.get_sessions():
            session.talk_enabled = True
            session.listen_enabled = True

    async def hangup(self, call_uuid: Optional[str] = None) -> bool:
        if call_uuid:
            session = self.get_session(call_uuid)
        else:
            sessions = self.get_sessions()
            session = sessions[0] if len(sessions) == 1 else None
        if session is None:
            return False
        if session.call_uuid:
            self._manual_hangups.add(session.call_uuid)
        await session.close()
        return True

    async def hangup_all(self) -> int:
        sessions = self.get_sessions()
        for session in sessions:
            if session.call_uuid:
                self._manual_hangups.add(session.call_uuid)
            await session.close()
        return len(sessions)

    async def _notify(self, message: str, workspace_ids=None) -> None:
        sent: set[int] = set()
        ids = self._normalize_workspace_ids(workspace_ids) if workspace_ids is not None else self._normalize_workspace_ids(None)
        for wid in ids:
            try:
                _, _, _, text_channel_id = self._workspace_voice_config(wid)
                if not text_channel_id or text_channel_id in sent:
                    continue
                channel = self.bot.get_channel(text_channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(text_channel_id)
                if hasattr(channel, "send"):
                    await channel.send(message)
                    sent.add(text_channel_id)
            except Exception:
                log.exception("Could not send bridge notification for workspace %s", wid)

    def status_dict(self) -> dict:
        sessions = [s for s in self.get_sessions() if s.active]
        calls = []
        for session in sessions:
            calls.append({
                "uuid": session.call_uuid,
                "short_uuid": (session.call_uuid or "")[:8],
                "direction": getattr(session, "direction", "inbound"),
                "number": getattr(session, "remote_number", ""),
                "caller_id": getattr(session, "caller_id", ""),
                "contact_name": getattr(session, "contact_name", ""),
                "age_seconds": round(session.age_seconds, 1),
                "pbx_to_discord_bytes": session.rx_audio_bytes,
                "discord_to_pbx_bytes": session.tx_audio_bytes,
                "dtmf": "".join(session.dtmf_digits),
                "listen_enabled": bool(session.listen_enabled),
                "talk_enabled": bool(session.talk_enabled),
                "source": getattr(session, "source", ""),
                "conference_group": getattr(session, "conference_group", ""),
                "conference_peer_frames_rx": int(getattr(session, "conference_peer_frames_rx", 0) or 0),
                "conference_peer_sources": len(getattr(session, "conference_peer_sources", set()) or set()),
                "randomize_caller_id": bool(getattr(session, "randomize_caller_id", False)),
                "retry_index": int(getattr(session, "retry_index", 0) or 0),
                "voicemail_detection_enabled": bool(getattr(session, "voicemail_detection_enabled", False)),
                "voicemail_detection_state": str(getattr(session, "voicemail_detection_state", "off")),
                "voicemail_detection_result": str(getattr(session, "voicemail_detection_result", "")),
                "workspace_ids": list(getattr(session, "workspace_ids", [])),
                "operator_user_id": str(getattr(session, "operator_user_id", "")),
                "operator_name": str(getattr(session, "operator_name", "")),
                "held": bool(getattr(session, "held", False)),
                "park_slot": int(getattr(session, "park_slot", 0) or 0),
            })

        workspace_voice = []
        if self.workspace_provider is not None:
            for ws in self.workspace_provider.db.list_workspaces():
                guild = self.bot.get_guild(int(ws["guild_id"])) if self.bot.is_ready() else None
                vc = guild.voice_client if guild else None
                workspace_voice.append({
                    "id": ws["id"], "alias": ws["alias"], "guild_id": ws["guild_id"],
                    "connected": bool(vc and vc.is_connected()),
                    "channel": vc.channel.name if vc and vc.is_connected() else None,
                    "voice_channel_id": ws.get("voice_channel_id", ""),
                })
        elif self.config.guild_id:
            guild = self.bot.get_guild(self.config.guild_id) if self.bot.is_ready() else None
            vc = guild.voice_client if guild else None
            workspace_voice.append({"id": "", "alias": "Main", "guild_id": str(self.config.guild_id), "connected": bool(vc and vc.is_connected()), "channel": vc.channel.name if vc and vc.is_connected() else None})

        outbound_pending = self.outbound_pending()
        return {
            "version": getattr(self.config, "version", "3.3.0"),
            "discord_ready": bool(self.bot.is_ready()),
            "discord_connected": any(x["connected"] for x in workspace_voice),
            "discord_channel": next((x["channel"] for x in workspace_voice if x["connected"]), None),
            "discord_workspaces": workspace_voice,
            "audiosocket": f"{self.config.audiosocket_bind}:{self.config.audiosocket_port}",
            "call_active": bool(calls),
            "call_count": len(calls),
            "calls": calls,
            "outbound_ringback_active": bool(self._ringback_calls),
            "ringback_muted": self.ringback_muted,
            "caller_to_discord_gain": self.pbx_to_discord_master_gain,
            "discord_to_caller_gain": self.discord_to_pbx_master_gain,
            "inbound_chime_gain": self.inbound_chime_master_gain,
            "outbound_pending": outbound_pending,
            "outbound_pending_count": len(outbound_pending),
            "call": calls[0] if len(calls) == 1 else None,
            "history": list(self.history)[:20],
        }

    def status_text(self) -> str:
        status = self.status_dict()
        voices = [x for x in status["discord_workspaces"] if x["connected"]]
        voice = ", ".join(f"{x['alias']}:{x['channel']}" for x in voices) or "not connected"
        lines = [
            f"Discord voice: **{voice}**",
            f"AudioSocket: **{status['audiosocket']}**",
            f"PBX calls: **{status['call_count']} active**",
        ]
        for call in status["calls"][:8]:
            label = call["number"] or call["short_uuid"]
            routes = f"caller={'on' if call['listen_enabled'] else 'muted'}, discord={'on' if call['talk_enabled'] else 'muted'}"
            lines.append(f"• `{call['short_uuid']}` {call['direction']} **{label}** ({call['age_seconds']:.0f}s; {routes})")
        return "\n".join(lines)
