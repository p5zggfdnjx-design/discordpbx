"""Canonical DiscordPBX bridge API.

The stable call/audio routing implementation lives in :mod:`bridge_core` while
Discord connection lifecycle is owned by :class:`voice_lifecycle.ReliableBridgeManager`.
``voice_resilience.VoiceResilientBridgeManager`` extends that single owner with
fast worker repair and stall forensics; it does not create a second reconnect
state machine.
"""

from bridge_core import *  # noqa: F401,F403
from bridge_core import BridgeManager as _CoreBridgeManager
from bridge_core import DiscordAudioSink, PBXAudioSource, utc_now

# Publish the core class during lifecycle module import so its subclass can
# import the public bridge primitives without a circular initialization failure.
BridgeManager = _CoreBridgeManager

from voice_lifecycle import ReliableBridgeManager  # noqa: E402,F401
from voice_resilience import VoiceResilientBridgeManager  # noqa: E402

BridgeManager = VoiceResilientBridgeManager

__all__ = [
    "BridgeManager",
    "DiscordAudioSink",
    "PBXAudioSource",
    "utc_now",
]
