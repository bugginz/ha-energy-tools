"""DDP sender (PLAN.md §8.5). One UDP packet per node per frame.

Header, 10 bytes (http://www.3waylabs.com/ddp/):

    flags(1) | seq(1) | type(1) | dest(1) | offset(4 BE) | len(2 BE)

flags 0x41 = version 1 | push. seq cycles 1..15 (low nibble per spec;
0 means "not used"). type 0x0B = RGB, 8 bits/channel. dest 1 = default
output device. At 240 LEDs a frame is 720 B — one packet — but the
splitter handles longer strips anyway.
"""

from __future__ import annotations

import socket
import struct

DDP_PORT = 4048
DDP_FLAGS_V1 = 0x40
DDP_FLAG_PUSH = 0x01
DDP_TYPE_RGB8 = 0x0B
DDP_DEST_DEFAULT = 0x01
MAX_DATA = 1440          # fits a normal-MTU UDP packet with the header


def ddp_packet(seq: int, offset: int, payload: bytes, push: bool) -> bytes:
    flags = DDP_FLAGS_V1 | (DDP_FLAG_PUSH if push else 0)
    return struct.pack("!BBBBIH", flags, seq & 0x0F, DDP_TYPE_RGB8,
                       DDP_DEST_DEFAULT, offset, len(payload)) + payload


class DDPSender:
    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._seq: dict[tuple[str, int], int] = {}   # per destination

    def send_frame(self, host: str, port: int, payload: bytes) -> None:
        # Sequence numbers are per node: each receiver checks continuity of
        # what *it* gets, so a shared counter across nodes would make every
        # packet look like a gap.
        key = (host, port)
        seq = self._seq.get(key, 0) % 15 + 1    # 1..15, skip 0
        self._seq[key] = seq
        offset = 0
        while True:
            chunk = payload[offset:offset + MAX_DATA]
            last = offset + len(chunk) >= len(payload)
            pkt = ddp_packet(seq, offset, chunk, push=last)
            try:
                self._sock.sendto(pkt, (host, port))
            except OSError:
                return                          # node offline; next frame retries
            if last:
                return
            offset += len(chunk)

    def close(self) -> None:
        self._sock.close()
