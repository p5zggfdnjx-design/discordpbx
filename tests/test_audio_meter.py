import struct
import time

import audio_meter


def pcm16(value: int, samples: int = 160) -> bytes:
    value = max(-32768, min(32767, int(value)))
    return b"".join(struct.pack("<h", value) for _ in range(samples))


def test_measure_silence_is_inactive():
    level = audio_meter._measure_pcm(b"\x00\x00" * 160)
    assert level["active"] is False
    assert level["rms_dbfs"] <= -80
    assert level["peak_dbfs"] <= -80


def test_measure_pcm_tracks_audible_signal():
    level = audio_meter._measure_pcm(pcm16(12000))
    assert level["active"] is True
    assert -10 < level["rms_dbfs"] < -7
    assert -10 < level["peak_dbfs"] < -7


def test_conditioner_limits_peak_after_makeup_gain():
    conditioned = audio_meter._apply_gain_and_limit(pcm16(24000), 2.0, 25500)
    level = audio_meter._measure_pcm(conditioned)
    assert level["peak_dbfs"] < -2.0
    assert level["peak_dbfs"] > -3.0


def test_stale_level_decays_to_inactive():
    level = {"rms_dbfs": -12.0, "peak_dbfs": -3.0, "active": True, "updated": time.monotonic() - 2.0}
    assert audio_meter._fresh(level, time.monotonic()) == {
        "rms_dbfs": -90.0,
        "peak_dbfs": -90.0,
        "active": False,
    }


def test_combine_uses_loudest_active_measurement():
    out = audio_meter._combine([
        {"rms_dbfs": -28.0, "peak_dbfs": -12.0, "active": True},
        {"rms_dbfs": -18.0, "peak_dbfs": -4.0, "active": True},
    ])
    assert out == {"rms_dbfs": -18.0, "peak_dbfs": -4.0, "active": True}


def test_meter_ui_injection_is_idempotent_and_contains_gain_mixer():
    page = '<html><head></head><body><div class="commandbar"></div></body></html>'
    once = audio_meter.inject_meter_ui(page)
    twice = audio_meter.inject_meter_ui(once)
    assert once == twice
    assert once.count('id="liveAudioMeters"') == 1
    assert once.count('id="liveAudioMeterScript"') == 1
    assert 'PHONE → DISCORD' in once
    assert 'DISCORD → PHONE' in once
    assert 'id="gainPhoneIn"' in once
    assert 'id="gainDiscordOut"' in once
    assert 'X-CSRF-Token' in once
    assert '/api/operator/audio' in once
    assert 'caller_to_discord_gain' in once
    assert 'discord_to_caller_gain' in once
