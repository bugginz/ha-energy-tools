"""RD-03D frame parsing, sensor->room transform, sources and recording.

Frame format (30 bytes, verify against docs.ai-thinker.com/en/Rd-03D_V2):

    AA FF 03 00 | 3 x [x i16, y i16, speed i16, dist_res u16] | 55 CC

x/y in mm, speed in cm/s, all little-endian. The sign encoding is NOT
two's complement: bit 15 set means POSITIVE, clear means negative, with
the magnitude in the low 15 bits — see s15(). An all-zero 8-byte block
means "no target in that slot".

Sensor frame: X lateral (right positive), Y forward from the sensor face,
origin at the sensor. Everything downstream of Pose.to_room() is room
coordinates in mm.
"""

from __future__ import annotations

import json
import math
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

FRAME_LEN = 30
HEADER = b"\xaa\xff\x03\x00"
TAIL = b"\x55\xcc"

# Mode commands, send on boot, expect an ack frame (FD FC FB FA ... 04 03 02 01)
CMD_MULTI_TARGET = bytes.fromhex("FDFCFBFA0200900004030201")
CMD_SINGLE_TARGET = bytes.fromhex("FDFCFBFA0200800004030201")
ACK_HEADER = b"\xfd\xfc\xfb\xfa"


def s15(raw: int) -> int:
    """Decode the RD-03D sign-flag int16: bit15 1=positive, 0=negative."""
    mag = raw & 0x7FFF
    return mag if raw & 0x8000 else -mag


@dataclass
class RadarTarget:
    x_mm: float
    y_mm: float
    speed_cms: float
    dist_res: int


@dataclass
class RadarFrame:
    ts: float
    targets: list[RadarTarget] = field(default_factory=list)


def parse_frame(buf: bytes, ts: float | None = None) -> RadarFrame | None:
    """Parse one 30-byte frame; None if header/tail/length don't check out."""
    if len(buf) != FRAME_LEN or buf[:4] != HEADER or buf[28:30] != TAIL:
        return None
    frame = RadarFrame(ts=time.time() if ts is None else ts)
    for i in range(3):
        off = 4 + 8 * i
        block = buf[off:off + 8]
        if block == b"\x00" * 8:
            continue
        rx, ry, rv, res = struct.unpack("<HHHH", block)
        frame.targets.append(RadarTarget(
            x_mm=float(s15(rx)), y_mm=float(s15(ry)),
            speed_cms=float(s15(rv)), dist_res=res))
    return frame


class FrameSync:
    """Byte-stream resynchroniser for the serial path.

    feed() returns every complete valid frame found; on a bad tail it
    skips one byte past the false header and keeps scanning.
    """

    MAX_BUF = 4 * FRAME_LEN

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        frames = []
        while True:
            idx = self._buf.find(HEADER)
            if idx < 0:
                # keep a header-1 tail in case the header straddles reads
                self._buf = self._buf[-(len(HEADER) - 1):]
                break
            self._buf = self._buf[idx:]
            if len(self._buf) < FRAME_LEN:
                break
            candidate = self._buf[:FRAME_LEN]
            if candidate[28:30] == TAIL:
                frames.append(candidate)
                self._buf = self._buf[FRAME_LEN:]
            else:
                self._buf = self._buf[1:]
        if len(self._buf) > self.MAX_BUF:
            self._buf = self._buf[-self.MAX_BUF:]
        return frames


@dataclass
class Pose:
    """Sensor->room rigid transform: room = R(theta) @ sensor + (tx, ty)."""

    theta_rad: float
    tx_mm: float
    ty_mm: float

    @classmethod
    def from_cfg(cls, pose_cfg) -> "Pose":
        return cls(theta_rad=math.radians(float(pose_cfg.theta_deg)),
                   tx_mm=float(pose_cfg.tx_mm), ty_mm=float(pose_cfg.ty_mm))

    def to_room(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        c, s = math.cos(self.theta_rad), math.sin(self.theta_rad)
        return (c * x_mm - s * y_mm + self.tx_mm,
                s * x_mm + c * y_mm + self.ty_mm)


def solve_pose(sensor_pts, room_pts) -> tuple[float, float, float]:
    """Least-squares 2D rigid fit (Procrustes) for §9.2 calibration.

    sensor_pts/room_pts: N>=2 corresponding (x, y) pairs in mm.
    Returns (theta_deg, tx_mm, ty_mm) for config, plus prints nothing —
    compute residuals yourself with Pose if you want them.
    """
    import numpy as np

    a = np.asarray(sensor_pts, dtype=float)
    b = np.asarray(room_pts, dtype=float)
    if a.shape != b.shape or a.shape[0] < 2:
        raise ValueError("need >= 2 corresponding points")
    ca, cb = a.mean(axis=0), b.mean(axis=0)
    h = (a - ca).T @ (b - cb)
    u, _, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T))
    r = vt.T @ np.diag([1.0, d]) @ u.T
    t = cb - r @ ca
    theta = math.degrees(math.atan2(r[1, 0], r[0, 0]))
    return theta, float(t[0]), float(t[1])


class Recorder:
    """Raw-frame JSONL recorder: one file per day, pruned to keep_days."""

    def __init__(self, directory: str, keep_days: int = 14):
        self.dir = directory
        self.keep_days = keep_days
        self._day = ""
        self._fh = None
        os.makedirs(directory, exist_ok=True)

    def record(self, ts: float, raw: bytes) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day != self._day:
            if self._fh:
                self._fh.close()
            self._day = day
            self._fh = open(os.path.join(self.dir, f"radar-{day}.jsonl"), "a")
            self._prune()
        self._fh.write(json.dumps({"ts": round(ts, 3), "hex": raw.hex()}) + "\n")
        self._fh.flush()

    def _prune(self) -> None:
        cutoff = time.time() - self.keep_days * 86400
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name)
            if name.startswith("radar-") and os.path.getmtime(path) < cutoff:
                os.unlink(path)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def replay_frames(path: str):
    """Yield (ts, raw_bytes) from a Recorder log."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            yield float(rec["ts"]), bytes.fromhex(rec["hex"])


class UdpRadarSource(threading.Thread):
    """Receives raw frames (and heartbeats) forwarded by the radar node.

    Raw 30-byte frames go to on_frame(ts, raw); JSON payloads (starting
    '{') are node heartbeats and go to on_heartbeat(ts, dict).
    """

    def __init__(self, port: int, on_frame, on_heartbeat=None):
        super().__init__(daemon=True, name="radar-udp")
        self.port = port
        self.on_frame = on_frame
        self.on_heartbeat = on_heartbeat
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", port))
        self._sock.settimeout(0.5)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            ts = time.time()
            if data[:1] == b"{":
                if self.on_heartbeat:
                    try:
                        self.on_heartbeat(ts, json.loads(data))
                    except (ValueError, UnicodeDecodeError):
                        pass
            elif len(data) == FRAME_LEN:
                self.on_frame(ts, data)

    def stop(self) -> None:
        self._stop.set()
        self._sock.close()


class SerialRadarSource(threading.Thread):
    """Reads the RD-03D directly over a USB-serial adapter (config
    radar.source: serial). Sends the multi-target mode command on open."""

    def __init__(self, port: str, baud: int, on_frame):
        super().__init__(daemon=True, name="radar-serial")
        self.port = port
        self.baud = baud
        self.on_frame = on_frame
        self._stop = threading.Event()

    def run(self) -> None:
        import serial  # lazy: pyserial only needed on this path

        sync = FrameSync()
        while not self._stop.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=0.2) as ser:
                    ser.write(CMD_MULTI_TARGET)
                    while not self._stop.is_set():
                        data = ser.read(256)
                        if not data:
                            continue
                        ts = time.time()
                        for raw in sync.feed(data):
                            self.on_frame(ts, raw)
            except (OSError, serial.SerialException):
                time.sleep(2.0)  # unplugged / enumerating; retry

    def stop(self) -> None:
        self._stop.set()
