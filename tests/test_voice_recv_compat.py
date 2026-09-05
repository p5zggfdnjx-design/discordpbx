from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

import voice_recv_compat


class _Decoder:
    def __init__(self, data=None, error: Exception | None = None):
        self.data = data
        self.error = error

    def pop_data(self):
        if self.error is not None:
            raise self.error
        return self.data


class VoiceRecvCompatTests(unittest.TestCase):
    def test_decoder_exception_is_isolated(self):
        router = SimpleNamespace(_lock=threading.Lock(), sink=SimpleNamespace(write=lambda *_: None))
        ok = voice_recv_compat._process_decoder(router, _Decoder(error=RuntimeError("bad packet")))
        self.assertFalse(ok)
        self.assertGreaterEqual(voice_recv_compat.diagnostics()["recovered_errors"], 1)

    def test_sink_runs_after_router_lock_is_released(self):
        lock = threading.Lock()
        observed = {"lock_free": False, "writes": 0}

        class Sink:
            def write(self, source, data):
                acquired = lock.acquire(blocking=False)
                observed["lock_free"] = acquired
                if acquired:
                    lock.release()
                observed["writes"] += 1

        data = SimpleNamespace(source=SimpleNamespace(id=123), pcm=b"\x00" * 8)
        router = SimpleNamespace(_lock=lock, sink=Sink())
        ok = voice_recv_compat._process_decoder(router, _Decoder(data=data))

        self.assertTrue(ok)
        self.assertTrue(observed["lock_free"])
        self.assertEqual(observed["writes"], 1)

    def test_sink_exception_does_not_escape(self):
        class Sink:
            def write(self, source, data):
                raise ValueError("DSP failure")

        data = SimpleNamespace(source=SimpleNamespace(id=456), pcm=b"\x00" * 8)
        router = SimpleNamespace(_lock=threading.Lock(), sink=Sink())
        ok = voice_recv_compat._process_decoder(router, _Decoder(data=data))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
