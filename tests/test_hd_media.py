from __future__ import annotations

import os
import struct
import uuid
from unittest.mock import patch

from media_config import MediaTransportConfig
from media_core import DISCORD_FRAME_BYTES, PcmMediaSession, frame_bytes
from pbx import build_websocket_media_dial_data
from websocket_media import control_event, parse_control_message


class Config:
    pbx_to_discord_gain = 1.0
    discord_to_pbx_gain = 1.0


class Manager:
    config = Config()

    def publish_pbx_frame(self, session, frame):
        pass

    def push_conference_pcm(self, call_uuid, frame):
        pass

    async def voicemail_classified(self, session, result, cause):
        pass


class Session(PcmMediaSession):
    def __init__(self, rate=16000):
        super().__init__(Manager(), media_transport="websocket", media_format=f"slin{rate // 1000}", sample_rate=rate)
        self.call_uuid = str(uuid.uuid4())


def stereo_frame(value: int = 8000) -> bytes:
    sample = struct.pack("<h", value)
    return (sample + sample) * 960  # 20 ms @ 48 kHz stereo


def test_true_hd_frame_sizes_are_real_20ms_pcm():
    assert frame_bytes(8000) == 320
    assert frame_bytes(16000) == 640
    assert frame_bytes(48000) == 1920


def test_canonical_engine_resamples_discord_48k_to_slin16():
    session = Session(16000)
    session.push_discord_pcm(123, stereo_frame())
    frame = session.next_outbound_frame()
    assert len(frame) == 640
    assert any(frame)
    assert session.media_transport == "websocket"
    assert session.media_wideband is True


def test_canonical_engine_can_run_fullband_slin48_without_resampling_down_to_8k():
    session = Session(48000)
    session.push_discord_pcm(123, stereo_frame())
    frame = session.next_outbound_frame()
    assert len(frame) == 1920
    assert any(frame)
    assert session.media_sample_rate == 48000


def test_pbx_wideband_input_is_preserved_until_internal_48k_boundary():
    session = Session(16000)
    mono = struct.pack("<h", 7000) * 320  # 20 ms @ 16 kHz mono
    session._feed_pbx_audio(mono, 16000)
    discord = session.read_discord_frame()
    assert len(discord) == DISCORD_FRAME_BYTES
    assert any(discord)
    assert session.media_rx_rate == 16000


def test_plain_text_media_start_parser():
    event = parse_control_message(
        "MEDIA_START connection_id:abc channel:WebSocket/discordpbx_media "
        "channel_id:pbx-1 format:slin16 optimal_frame_size:640 ptime:20"
    )
    assert control_event(event) == "MEDIA_START"
    assert event["format"] == "slin16"
    assert event["optimal_frame_size"] == "640"


def test_json_media_start_parser():
    event = parse_control_message(
        '{"event":"MEDIA_START","format":"slin48","optimal_frame_size":1920,"ptime":20}'
    )
    assert control_event(event) == "MEDIA_START"
    assert event["format"] == "slin48"
    assert event["optimal_frame_size"] == 1920


def test_flow_control_events_parse():
    assert control_event(parse_control_message("MEDIA_XOFF")) == "MEDIA_XOFF"
    assert control_event(parse_control_message('{"event":"MEDIA_XON"}')) == "MEDIA_XON"


def test_dial_target_is_slin16_and_correlated_by_uuid():
    call_uuid = str(uuid.uuid4())
    dial = build_websocket_media_dial_data("discordpbx_media", "slin16", call_uuid)
    assert dial == f"WebSocket/discordpbx_media/c(slin16)v(call_uuid={call_uuid})"


def test_json_control_is_requested_only_when_configured():
    call_uuid = str(uuid.uuid4())
    dial = build_websocket_media_dial_data("discordpbx_media", "slin16", call_uuid, "json")
    assert "f(json)" in dial


def test_dial_target_rejects_unsafe_connection_name():
    call_uuid = str(uuid.uuid4())
    try:
        build_websocket_media_dial_data("bad/name", "slin16", call_uuid)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe connection id was accepted")


def test_auto_mode_preserves_existing_installs_until_secure_hd_credentials_exist():
    with patch.dict(os.environ, {}, clear=True):
        cfg = MediaTransportConfig.from_env()
        assert cfg.transport == "auto"
        assert cfg.use_websocket_for_outbound is False
        assert cfg.websocket_server_enabled is False


def test_auto_mode_selects_true_hd_when_secure_media_credentials_exist():
    env = {
        "PBX_MEDIA_TRANSPORT": "auto",
        "MEDIA_WS_USERNAME": "discordpbx",
        "MEDIA_WS_PASSWORD": "a-long-random-secret",
        "ASTERISK_MEDIA_CONNECTION": "discordpbx_media",
        "MEDIA_WS_FORMAT": "slin16",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = MediaTransportConfig.from_env()
        assert cfg.use_websocket_for_outbound is True
        assert cfg.websocket_server_enabled is True
        assert cfg.websocket_rate == 16000


def test_invalid_requested_format_does_not_silently_become_fake_hd():
    with patch.dict(os.environ, {"MEDIA_WS_FORMAT": "ulaw"}, clear=True):
        cfg = MediaTransportConfig.from_env()
        # Unsupported values fail back to the project's actual PCM HD profile,
        # never to an arbitrary relabeled sample rate.
        assert cfg.websocket_format == "slin16"
        assert cfg.websocket_rate == 16000
