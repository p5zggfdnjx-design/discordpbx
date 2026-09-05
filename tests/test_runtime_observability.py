from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from runtime_observability import audio_format_summary, inject_observability_ui, lag_summary


class FakeSession:
    def __init__(self, transport: str, fmt: str, rx: int, tx: int, active: bool = True):
        self.media_transport = transport
        self.media_format = fmt
        self.media_rx_rate = rx
        self.media_tx_rate = tx
        self.active = active


class DoneTask:
    def __init__(self, done: bool):
        self._done = done

    def done(self):
        return self._done


class FakeManager:
    def __init__(self, sessions=None, reliability=None, monitor_active=True):
        self._sessions = list(sessions or [])
        self._reliability = dict(reliability or {})
        self._event_loop_monitor = DoneTask(not monitor_active) if monitor_active is not None else None

    def get_sessions(self):
        return list(self._sessions)


def clean_media_env():
    return {
        "PBX_MEDIA_TRANSPORT": "auto",
        "MEDIA_WS_USERNAME": "",
        "MEDIA_WS_PASSWORD": "",
        "ASTERISK_MEDIA_CONNECTION": "discordpbx_media",
        "MEDIA_WS_FORMAT": "slin16",
    }


class RuntimeObservabilityTests(unittest.TestCase):
    def test_live_slin16_websocket_is_confirmed_hd(self):
        manager = FakeManager([FakeSession("websocket", "slin16", 16000, 16000)])
        with patch.dict(os.environ, clean_media_env(), clear=True):
            summary = audio_format_summary(manager)
        self.assertTrue(summary["confirmed"])
        self.assertEqual(summary["quality"], "hd")
        self.assertEqual(summary["transport"], "websocket")
        self.assertEqual(summary["pbx_rx_rates_hz"], [16000])
        self.assertEqual(summary["pbx_tx_rates_hz"], [16000])

    def test_live_8k_audiosocket_is_not_called_hd(self):
        manager = FakeManager([FakeSession("audiosocket", "slin", 8000, 8000)])
        with patch.dict(os.environ, clean_media_env(), clear=True):
            summary = audio_format_summary(manager)
        self.assertTrue(summary["confirmed"])
        self.assertEqual(summary["quality"], "voice")
        self.assertFalse(summary["calls"][0]["wideband"])

    def test_idle_can_report_wideband_config_without_claiming_live_proof(self):
        env = clean_media_env()
        env.update({
            "PBX_MEDIA_TRANSPORT": "auto",
            "MEDIA_WS_USERNAME": "discordpbx",
            "MEDIA_WS_PASSWORD": "long-random-secret",
            "MEDIA_WS_FORMAT": "slin16",
        })
        with patch.dict(os.environ, env, clear=True):
            summary = audio_format_summary(FakeManager())
        self.assertFalse(summary["confirmed"])
        self.assertTrue(summary["configured"]["wideband_preferred"])
        self.assertEqual(summary["configured"]["rate_hz"], 16000)

    def test_lag_detector_thresholds_and_monitor_state(self):
        manager = FakeManager(monitor_active=True)
        warning = lag_summary(manager, {"voice_reliability": {"event_loop_lag_seconds": 0.4, "event_loop_lag_max_seconds": 0.8, "event_loop_lag_warnings": 0}})
        critical = lag_summary(manager, {"voice_reliability": {"event_loop_lag_seconds": 1.2, "event_loop_lag_max_seconds": 1.2, "event_loop_lag_warnings": 1}})
        self.assertEqual(warning["state"], "warning")
        self.assertEqual(critical["state"], "critical")
        self.assertEqual(critical["stall_count"], 1)

    def test_ui_injection_is_idempotent(self):
        page = '<html><head></head><body><div class="commandbar"></div></body></html>'
        once = inject_observability_ui(page)
        twice = inject_observability_ui(once)
        self.assertIn('id="runtimeHealthStrip"', once)
        self.assertIn('id="audioPathPill"', once)
        self.assertIn('id="eventLoopLagPill"', once)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
