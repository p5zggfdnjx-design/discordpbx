"""Canonical DiscordPBX bridge API.

The stable call/audio routing implementation lives in :mod:`bridge_core` while
Discord connection lifecycle is owned by :class:`voice_lifecycle.ReliableBridgeManager`.
This split is intentional: voice reconnect/prewarm/idle state is normal source
architecture, not a runtime class monkeypatch.
"""

from bridge_core import *  # noqa: F401,F403
from bridge_core import BridgeManager as _CoreBridgeManager
from bridge_core import DiscordAudioSink, PBXAudioSource, utc_now

# Publish the core class during the lifecycle module import so its subclass can
# import the public bridge primitives without a circular initialization failure.
BridgeManager = _CoreBridgeManager

from voice_lifecycle import ReliableBridgeManager  # noqa: E402

BridgeManager = ReliableBridgeManager

__all__ = [
    "BridgeManager",
    "DiscordAudioSink",
    "PBXAudioSource",
    "utc_now",
]
