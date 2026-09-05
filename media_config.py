from __future__ import annotations

import os
import re
from dataclasses import dataclass


_CONNECTION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
LINEAR_FORMAT_RATES = {
    "slin": 8000,
    "slin12": 12000,
    "slin16": 16000,
    "slin24": 24000,
    "slin32": 32000,
    "slin44": 44100,
    "slin48": 48000,
}


@dataclass(frozen=True)
class MediaTransportConfig:
    """Configuration for the Asterisk <-> DiscordPBX media boundary.

    The existing AudioSocket transport remains a compatibility fallback. True
    wideband media uses Asterisk chan_websocket with signed-linear PCM so there is
    no 8 kHz AudioSocket application bottleneck inside DiscordPBX.
    """

    transport: str = "auto"  # auto | websocket | audiosocket
    websocket_bind: str = "0.0.0.0"
    websocket_port: int = 9093
    websocket_username: str = "discordpbx"
    websocket_password: str = ""
    websocket_format: str = "slin16"
    websocket_control_format: str = "plain-text"  # plain-text | json
    asterisk_connection_id: str = "discordpbx_media"

    @classmethod
    def from_env(cls) -> "MediaTransportConfig":
        transport = (os.getenv("PBX_MEDIA_TRANSPORT", "auto").strip().lower() or "auto")
        if transport not in {"auto", "websocket", "audiosocket"}:
            transport = "auto"

        fmt = (os.getenv("MEDIA_WS_FORMAT", "slin16").strip().lower() or "slin16")
        if fmt not in LINEAR_FORMAT_RATES:
            fmt = "slin16"

        control = (os.getenv("MEDIA_WS_CONTROL_FORMAT", "plain-text").strip().lower() or "plain-text")
        if control not in {"plain-text", "json"}:
            control = "plain-text"

        try:
            port = int(os.getenv("MEDIA_WS_PORT", "9093").strip() or "9093")
        except ValueError:
            port = 9093
        port = max(1, min(65535, port))

        connection_id = (os.getenv("ASTERISK_MEDIA_CONNECTION", "discordpbx_media").strip() or "discordpbx_media")
        if not _CONNECTION_ID.fullmatch(connection_id):
            connection_id = "discordpbx_media"

        return cls(
            transport=transport,
            websocket_bind=(os.getenv("MEDIA_WS_BIND", "0.0.0.0").strip() or "0.0.0.0"),
            websocket_port=port,
            websocket_username=(os.getenv("MEDIA_WS_USERNAME", "discordpbx").strip() or "discordpbx"),
            websocket_password=os.getenv("MEDIA_WS_PASSWORD", ""),
            websocket_format=fmt,
            websocket_control_format=control,
            asterisk_connection_id=connection_id,
        )

    @property
    def websocket_rate(self) -> int:
        return LINEAR_FORMAT_RATES[self.websocket_format]

    @property
    def websocket_configured(self) -> bool:
        return bool(
            self.websocket_username
            and self.websocket_password
            and _CONNECTION_ID.fullmatch(self.asterisk_connection_id)
        )

    @property
    def websocket_server_enabled(self) -> bool:
        return self.transport != "audiosocket" and self.websocket_configured

    @property
    def use_websocket_for_outbound(self) -> bool:
        if self.transport == "audiosocket":
            return False
        if self.transport == "websocket":
            return True
        return self.websocket_configured

    def require_websocket_ready(self) -> None:
        if not self.websocket_configured:
            raise RuntimeError(
                "PBX WebSocket media is selected but MEDIA_WS_USERNAME / MEDIA_WS_PASSWORD "
                "or ASTERISK_MEDIA_CONNECTION is not configured"
            )


def validate_connection_id(value: str) -> str:
    value = str(value or "").strip()
    if not _CONNECTION_ID.fullmatch(value):
        raise ValueError("Asterisk media connection id contains invalid characters")
    return value
