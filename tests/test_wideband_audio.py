import os
import unittest
from unittest.mock import patch

import wideband_audio


class WidebandAudioTests(unittest.TestCase):
    def test_frame_sizes_are_20ms_signed_mono(self):
        self.assertEqual(wideband_audio.frame_bytes(8000), 320)
        self.assertEqual(wideband_audio.frame_bytes(16000), 640)
        self.assertEqual(wideband_audio.frame_bytes(48000), 1920)

    def test_auto_mirrors_supported_incoming_rate(self):
        self.assertEqual(wideband_audio.choose_auto_rate(8000), 8000)
        self.assertEqual(wideband_audio.choose_auto_rate(16000), 16000)
        self.assertEqual(wideband_audio.choose_auto_rate(48000), 48000)

    def test_auto_falls_back_to_classic_audiosocket_rate(self):
        self.assertEqual(wideband_audio.choose_auto_rate(None), 8000)
        self.assertEqual(wideband_audio.choose_auto_rate(11025), 8000)
        self.assertEqual(wideband_audio.choose_auto_rate("bad"), 8000)

    def test_packet_mapping_matches_asterisk_audiosocket_types(self):
        self.assertEqual(wideband_audio.RATE_TO_PACKET[8000], 0x10)
        self.assertEqual(wideband_audio.RATE_TO_PACKET[16000], 0x12)
        self.assertEqual(wideband_audio.RATE_TO_PACKET[48000], 0x16)

    def test_config_defaults_to_adaptive(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(wideband_audio._configured_rate())

    def test_config_accepts_supported_override_only(self):
        with patch.dict(os.environ, {"PBX_AUDIO_RATE": "16000"}, clear=True):
            self.assertEqual(wideband_audio._configured_rate(), 16000)
        with patch.dict(os.environ, {"PBX_AUDIO_RATE": "96000"}, clear=True):
            self.assertIsNone(wideband_audio._configured_rate())

    def test_quality_ui_injection_is_idempotent(self):
        page = '<html><head></head><body><div class="liveAudioMeters" id="liveAudioMeters" aria-label="Live call audio levels"></div></body></html>'
        first = wideband_audio.inject_quality_ui(page)
        second = wideband_audio.inject_quality_ui(first)
        self.assertIn('id="audioQualityBadge"', first)
        self.assertIn('id="audioQualityScript"', first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
