#!/usr/bin/env python3
"""Simulate an Asterisk AudioSocket call for a quick end-to-end bridge test."""

import argparse
import asyncio
import audioop
import math
import struct
import time
import uuid

FRAME_SAMPLES = 160  # 20ms @ 8kHz
FRAME_BYTES = FRAME_SAMPLES * 2


def packet(kind: int, payload: bytes = b"") -> bytes:
    return bytes((kind,)) + struct.pack(">H", len(payload)) + payload


def tone_frame(phase: int, hz: float = 440.0, gain: float = 0.15) -> tuple[bytes, int]:
    samples = []
    amp = int(32767 * gain)
    for i in range(FRAME_SAMPLES):
        n = phase + i
        samples.append(int(amp * math.sin(2 * math.pi * hz * n / 8000)))
    return struct.pack("<" + "h" * FRAME_SAMPLES, *samples), phase + FRAME_SAMPLES


async def reader_task(reader: asyncio.StreamReader, stats: dict):
    try:
        while True:
            head = await reader.readexactly(3)
            kind = head[0]
            length = struct.unpack(">H", head[1:])[0]
            payload = await reader.readexactly(length) if length else b""
            if kind == 0x00:
                return
            if kind == 0x10:
                stats["audio_bytes"] += len(payload)
                stats["audio_packets"] += 1
                if audioop.rms(payload, 2) > 100:
                    stats["non_silent_packets"] += 1
    except asyncio.IncompleteReadError:
        return


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", default="127.0.0.1")
    ap.add_argument("port", nargs="?", default=9092, type=int)
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()

    reader, writer = await asyncio.open_connection(args.host, args.port)
    call_id = uuid.uuid4()
    print(f"Connected; simulated call UUID: {call_id}")
    writer.write(packet(0x01, call_id.bytes))
    await writer.drain()

    stats = {"audio_bytes": 0, "audio_packets": 0, "non_silent_packets": 0}
    rt = asyncio.create_task(reader_task(reader, stats))

    start = time.monotonic()
    phase = 0
    next_tick = start
    while time.monotonic() - start < args.seconds:
        frame, phase = tone_frame(phase)
        writer.write(packet(0x10, frame))
        await writer.drain()
        next_tick += 0.020
        await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

    writer.write(packet(0x00))
    await writer.drain()
    await asyncio.sleep(0.1)
    writer.close()
    await writer.wait_closed()
    try:
        await asyncio.wait_for(rt, 1.0)
    except asyncio.TimeoutError:
        rt.cancel()

    print(f"Received from Discord side: {stats['audio_packets']} audio packets / {stats['audio_bytes']} bytes")
    print(f"Non-silent Discord->PBX packets: {stats['non_silent_packets']}")
    print("If you heard the tone in Discord, PBX->Discord works.")
    print("If non-silent packets increased while someone spoke in Discord, Discord->PBX receive works.")


if __name__ == "__main__":
    asyncio.run(main())
