from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import uuid
from typing import Any

from aiohttp import WSMsgType, web

from media_config import LINEAR_FORMAT_RATES, MediaTransportConfig
from media_core import PcmMediaSession, frame_bytes


log = logging.getLogger("discord-pbx.websocket-media")


def parse_control_message(text: str) -> dict[str, Any]:
    """Parse either chan_websocket plain-text or JSON control messages."""
    raw = str(text or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"event": "INVALID", "raw": raw}
        return value if isinstance(value, dict) else {"event": "INVALID", "raw": raw}

    parts = raw.split()
    event = parts[0]
    out: dict[str, Any] = {"event": event}
    for token in parts[1:]:
        if ":" in token:
            key, value = token.split(":", 1)
            out[key] = value
        elif event == "DTMF_END" and "digit" not in out:
            out["digit"] = token
    return out


def control_event(value: dict[str, Any]) -> str:
    return str(value.get("event") or value.get("command") or "").strip().upper()


def _basic_credentials(header: str) -> tuple[str, str]:
    raw = str(header or "")
    if not raw.startswith("Basic "):
        return "", ""
    try:
        decoded = base64.b64decode(raw[6:], validate=True).decode("utf-8")
        return tuple(decoded.split(":", 1)) if ":" in decoded else ("", "")
    except Exception:
        return "", ""


class WebSocketMediaSession(PcmMediaSession):
    """One true wideband Asterisk chan_websocket media channel."""

    def __init__(self, ws: web.WebSocketResponse, request: web.Request, manager, config: MediaTransportConfig):
        requested_uuid = str(request.query.get("call_uuid", "")).strip()
        try:
            self._requested_uuid = str(uuid.UUID(requested_uuid))
        except (ValueError, AttributeError) as exc:
            raise ValueError("call_uuid query parameter must be a valid UUID") from exc

        super().__init__(
            manager,
            media_transport="websocket",
            media_format=config.websocket_format,
            sample_rate=config.websocket_rate,
        )
        self.call_uuid = self._requested_uuid
        self.ws = ws
        self.request = request
        self.config = config
        self.peer = request.remote or ""
        self.channel = ""
        self.channel_id = ""
        self.connection_id = ""
        self.optimal_frame_size = frame_bytes(self.media_sample_rate)
        self.ptime = 20
        self.control_format = "plain-text"
        self._sender_task: asyncio.Task | None = None
        self._can_send = asyncio.Event()
        self._can_send.set()
        self._manager_started = False
        self._closed_by_us = False

    def _accept_media_start(self, event: dict[str, Any]) -> None:
        if control_event(event) != "MEDIA_START":
            raise RuntimeError("first chan_websocket control event must be MEDIA_START")
        fmt = str(event.get("format") or self.config.websocket_format).strip().lower()
        if fmt not in LINEAR_FORMAT_RATES:
            raise RuntimeError(
                f"unsupported Asterisk WebSocket media format {fmt!r}; configure signed-linear PCM such as slin16"
            )
        rate = LINEAR_FORMAT_RATES[fmt]
        if rate < 16000:
            log.warning(
                "WebSocket media call %s negotiated %s (%d Hz); transport works but this call is not HD",
                self.call_uuid, fmt, rate,
            )
        self.set_media_rate(rate, fmt)
        self.channel = str(event.get("channel") or "")
        self.channel_id = str(event.get("channel_id") or "")
        self.connection_id = str(event.get("connection_id") or "")
        try:
            self.optimal_frame_size = int(event.get("optimal_frame_size") or frame_bytes(rate))
        except (TypeError, ValueError):
            self.optimal_frame_size = frame_bytes(rate)
        try:
            self.ptime = int(event.get("ptime") or 20)
        except (TypeError, ValueError):
            self.ptime = 20
        self.control_format = "json" if isinstance(event.get("channel_variables"), dict) else "plain-text"

    async def _handle_control(self, text: str) -> None:
        event = parse_control_message(text)
        kind = control_event(event)
        if kind == "MEDIA_XOFF":
            self._can_send.clear()
            return
        if kind == "MEDIA_XON":
            self._can_send.set()
            return
        if kind == "DTMF_END":
            digit = str(event.get("digit") or "")[:1]
            if digit:
                self.dtmf_digits.append(digit)
                await self.manager.dtmf_received(self, digit)
            return
        if kind == "MEDIA_START" and not self._manager_started:
            self._accept_media_start(event)
            return
        if kind in {"STATUS", "QUEUE_DRAINED", "MEDIA_BUFFERING_COMPLETED", "MEDIA_MARK_PROCESSED"}:
            return
        if kind and kind != "INVALID":
            log.debug("Unhandled chan_websocket control event %s on %s", kind, self.call_uuid)

    async def _wait_for_media_start(self) -> None:
        while self.active:
            msg = await asyncio.wait_for(self.ws.receive(), timeout=5.0)
            if msg.type == WSMsgType.TEXT:
                event = parse_control_message(msg.data)
                if control_event(event) == "MEDIA_START":
                    self._accept_media_start(event)
                    return
                await self._handle_control(msg.data)
            elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                raise ConnectionError("Asterisk WebSocket closed before MEDIA_START")
            elif msg.type == WSMsgType.BINARY:
                raise RuntimeError("Asterisk sent media before MEDIA_START")
        raise ConnectionError("media session closed before MEDIA_START")

    async def _sender(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while self.active and not self.ws.closed:
            await self._can_send.wait()
            frame = self.next_outbound_frame()
            try:
                await self.ws.send_bytes(frame)
                self.tx_audio_bytes += len(frame)
                self.tx_packets += 1
            except (ConnectionError, asyncio.CancelledError):
                raise
            except Exception:
                log.exception("Failed sending WebSocket media for %s", self.call_uuid)
                return
            next_tick += 0.020
            delay = next_tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_tick = loop.time()

    async def run(self) -> None:
        try:
            await self._wait_for_media_start()
            accepted = await self.manager.call_started(self)
            if not accepted:
                await self._send_hangup()
                return
            self._manager_started = True
            log.info(
                "WebSocket media call %s connected from %s format=%s rate=%dHz frame=%d",
                self.call_uuid, self.peer, self.media_format, self.media_sample_rate, self.optimal_frame_size,
            )
            self._sender_task = asyncio.create_task(self._sender(), name=f"ws-media-send-{self.call_uuid}")

            async for msg in self.ws:
                if msg.type == WSMsgType.BINARY:
                    payload = bytes(msg.data)
                    if payload:
                        self.rx_packets += 1
                        self.rx_audio_bytes += len(payload)
                        self._feed_pbx_audio(payload, self.media_rx_rate)
                elif msg.type == WSMsgType.TEXT:
                    await self._handle_control(msg.data)
                elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("WebSocket media call failed (%s)", self.call_uuid or self.peer)
        finally:
            self.active = False
            self._can_send.set()
            if self._sender_task:
                self._sender_task.cancel()
                try:
                    await self._sender_task
                except asyncio.CancelledError:
                    pass
            if self._manager_started:
                await self.manager.call_ended(self)
                self._manager_started = False
            if not self.ws.closed:
                await self.ws.close()
            log.info("WebSocket media call %s ended", self.call_uuid or "unknown")

    async def _send_hangup(self) -> None:
        if self.ws.closed:
            return
        try:
            if self.control_format == "json":
                await self.ws.send_str(json.dumps({"command": "HANGUP"}, separators=(",", ":")))
            else:
                await self.ws.send_str("HANGUP")
        except Exception:
            pass

    async def close(self) -> None:
        if not self.active:
            return
        self.active = False
        self._closed_by_us = True
        self._can_send.set()
        await self._send_hangup()
        if not self.ws.closed:
            await self.ws.close()


class WebSocketMediaServer:
    def __init__(self, manager, config: MediaTransportConfig):
        self.manager = manager
        self.config = config
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    def _authorized(self, request: web.Request) -> bool:
        username, password = _basic_credentials(request.headers.get("Authorization", ""))
        return bool(
            username
            and password
            and hmac.compare_digest(username, self.config.websocket_username)
            and hmac.compare_digest(password, self.config.websocket_password)
        )

    async def start(self) -> None:
        if not self.config.websocket_server_enabled:
            return
        app = web.Application(client_max_size=65500)
        app.router.add_get("/media", self._media)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.config.websocket_bind, self.config.websocket_port)
        await self.site.start()
        log.info(
            "Asterisk WebSocket media listening on %s:%d (%s / %d Hz)",
            self.config.websocket_bind,
            self.config.websocket_port,
            self.config.websocket_format,
            self.config.websocket_rate,
        )

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    async def _media(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.Response(
                status=401,
                text="Asterisk media authentication required",
                headers={"WWW-Authenticate": 'Basic realm="DiscordPBX Media"'},
            )
        call_uuid = str(request.query.get("call_uuid", "")).strip()
        try:
            uuid.UUID(call_uuid)
        except (ValueError, AttributeError):
            return web.Response(status=400, text="valid call_uuid query parameter required")

        ws = web.WebSocketResponse(protocols=("media",), max_msg_size=65500, heartbeat=30.0)
        await ws.prepare(request)
        session = WebSocketMediaSession(ws, request, self.manager, self.config)
        await session.run()
        return ws
