"""Tests for the kickboard lighting service (kickboard/PLAN.md).

Stdlib unittest only (no pytest), same as the other test files:

    python3 -m unittest tests.test_kickboard -v

Covers the hardware-free pipeline (phases 1-2): RD-03D frame decoding
including the sign-flag gotcha, stream resync, the sensor->room transform
and its Procrustes solver, LED geometry interpolation, the renderer's
pool/gamma/budget behaviour, DDP packet layout, and tracker identity/
confidence/gating.
"""

import math
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kickboard"))

import numpy as np  # noqa: E402

from kickboard import config as config_mod  # noqa: E402
from kickboard import ddp, geometry, radar, tracker as tracker_mod  # noqa: E402
from kickboard.renderer import Renderer  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "kickboard" / "config.yaml"


def enc15(v: int) -> int:
    """Encode into the RD-03D sign-flag format (bit15 1=positive)."""
    return (0x8000 | v) if v >= 0 else (-v & 0x7FFF)


def make_frame(targets):
    """Build a synthetic 30-byte RD-03D frame from (x, y, speed) triples."""
    body = b""
    for x, y, v in targets:
        body += struct.pack("<HHHH", enc15(x), enc15(y), enc15(v), 320)
    body += b"\x00" * 8 * (3 - len(targets))
    return radar.HEADER + body + radar.TAIL


class S15Test(unittest.TestCase):
    """The sign encoding is NOT two's complement (PLAN.md §7)."""

    def test_bit15_set_is_positive(self):
        self.assertEqual(radar.s15(0x8000 | 1234), 1234)

    def test_bit15_clear_is_negative(self):
        self.assertEqual(radar.s15(1234), -1234)

    def test_zero(self):
        self.assertEqual(radar.s15(0x8000), 0)
        self.assertEqual(radar.s15(0x0000), 0)

    def test_roundtrip(self):
        for v in (-32767, -1, 0, 1, 5000, 32767):
            self.assertEqual(radar.s15(enc15(v)), v)


class ParseFrameTest(unittest.TestCase):
    def test_three_targets(self):
        frame = radar.parse_frame(make_frame([(100, 2000, -25),
                                              (-500, 1500, 0),
                                              (0, 3000, 12)]), ts=1.0)
        self.assertEqual(len(frame.targets), 3)
        t0, t1, t2 = frame.targets
        self.assertEqual((t0.x_mm, t0.y_mm, t0.speed_cms), (100, 2000, -25))
        self.assertEqual((t1.x_mm, t1.y_mm), (-500, 1500))
        self.assertEqual(t2.speed_cms, 12)

    def test_empty_slots_skipped(self):
        frame = radar.parse_frame(make_frame([(100, 2000, 0)]), ts=1.0)
        self.assertEqual(len(frame.targets), 1)

    def test_no_targets(self):
        frame = radar.parse_frame(make_frame([]), ts=1.0)
        self.assertEqual(frame.targets, [])

    def test_bad_frames_rejected(self):
        good = make_frame([(1, 1, 1)])
        self.assertIsNone(radar.parse_frame(good[:-1]))
        self.assertIsNone(radar.parse_frame(b"\x00" + good[1:]))
        self.assertIsNone(radar.parse_frame(good[:-2] + b"\x00\x00"))


class FrameSyncTest(unittest.TestCase):
    def test_resync_through_garbage(self):
        f1 = make_frame([(1, 100, 0)])
        f2 = make_frame([(2, 200, 0)])
        stream = b"\x12\x34" + f1 + b"\xaa\xff\x99" + f2 + f1[:11]
        sync = radar.FrameSync()
        got = []
        for i in range(0, len(stream), 7):   # drip-feed in odd chunks
            got += sync.feed(stream[i:i + 7])
        self.assertEqual(got, [f1, f2])
        got = sync.feed(f1[11:])             # rest of the split frame
        self.assertEqual(got, [f1])

    def test_false_header_with_bad_tail_skipped(self):
        f1 = make_frame([(5, 500, 0)])
        junk = radar.HEADER + b"\x00" * 26    # header, wrong tail
        sync = radar.FrameSync()
        self.assertEqual(sync.feed(junk + f1), [f1])


class PoseTest(unittest.TestCase):
    def test_identity(self):
        p = radar.Pose(0.0, 0.0, 0.0)
        self.assertEqual(p.to_room(100, 200), (100, 200))

    def test_rotate_minus_90_translate(self):
        # sensor at room (0, 900) facing +x: forward (0,1) -> room +x
        p = radar.Pose(math.radians(-90), 0.0, 900.0)
        x, y = p.to_room(0, 2000)            # 2 m dead ahead
        self.assertAlmostEqual(x, 2000, places=6)
        self.assertAlmostEqual(y, 900, places=6)
        x, y = p.to_room(500, 2000)          # and 0.5 m to sensor's right
        self.assertAlmostEqual(x, 2000, places=6)
        self.assertAlmostEqual(y, 400, places=6)

    def test_solve_pose_recovers_transform(self):
        true = radar.Pose(math.radians(-77.0), 123.0, 456.0)
        sensor = [(0, 1000), (800, 2500), (-600, 3100), (200, 400)]
        room = [true.to_room(*p) for p in sensor]
        theta, tx, ty = radar.solve_pose(sensor, room)
        self.assertAlmostEqual(theta, -77.0, places=6)
        self.assertAlmostEqual(tx, 123.0, places=4)
        self.assertAlmostEqual(ty, 456.0, places=4)


class SlantTest(unittest.TestCase):
    """Ceiling-mount slant-range -> floor projection (radar.slant_to_floor)."""

    def test_wall_mount_is_noop(self):
        self.assertEqual(radar.slant_to_floor(500, 2000, 0), (500, 2000))
        self.assertEqual(radar.slant_to_floor(500, 2000, -1000), (500, 2000))

    def test_projects_slant_onto_floor(self):
        # 2.4 m ceiling, 1.0 m torso -> h = 1400; person 3 m out on axis:
        # sensor reports slant sqrt(3000^2 + 1400^2) = 3310.6
        import math as m
        slant = m.hypot(3000, 1400)
        x, y = radar.slant_to_floor(0.0, slant, 1400.0)
        self.assertAlmostEqual(y, 3000.0, places=6)
        self.assertAlmostEqual(x, 0.0, places=6)
        # off-axis keeps its bearing
        x, y = radar.slant_to_floor(slant * 0.6, slant * 0.8, 1400.0)
        self.assertAlmostEqual(m.hypot(x, y), 3000.0, places=6)
        self.assertAlmostEqual(x / y, 0.75, places=6)

    def test_under_sensor_clamps_to_zero(self):
        x, y = radar.slant_to_floor(0.0, 1000.0, 1400.0)
        self.assertEqual((x, y), (0.0, 0.0))


class GeometryTest(unittest.TestCase):
    def test_straight_run(self):
        m = geometry.build_led_map([(0, 0, 50), (9, 900, 50)], 10)
        self.assertEqual(m.shape, (10, 2))
        self.assertTrue(np.allclose(m[:, 0], np.arange(10) * 100))
        self.assertTrue(np.allclose(m[:, 1], 50))

    def test_corner(self):
        m = geometry.build_led_map([(0, 0, 0), (4, 400, 0), (8, 400, 400)], 9)
        self.assertTrue(np.allclose(m[4], [400, 0]))
        self.assertTrue(np.allclose(m[6], [400, 200]))
        self.assertTrue(np.allclose(m[8], [400, 400]))

    def test_daisy_chain_gap(self):
        # one chain across the kitchen: LED 299 ends side A, LED 300 starts
        # side B — adjacent waypoint indices place no LEDs in the gap
        m = geometry.build_led_map(
            [(0, 100, 50), (299, 4083, 50),
             (300, 4083, 1750), (539, 100, 1750)], 540)
        self.assertTrue(np.allclose(m[299], [4083, 50]))
        self.assertTrue(np.allclose(m[300], [4083, 1750]))
        self.assertTrue(np.allclose(m[539], [100, 1750]))

    def test_validation(self):
        with self.assertRaises(ValueError):
            geometry.build_led_map([(1, 0, 0), (9, 900, 0)], 10)   # no LED 0
        with self.assertRaises(ValueError):
            geometry.build_led_map([(0, 0, 0), (8, 900, 0)], 10)   # short
        with self.assertRaises(ValueError):
            geometry.build_led_map([(0, 0, 0), (5, 1, 0), (5, 2, 0),
                                    (9, 900, 0)], 10)              # dup index

    def test_ascii_map_runs(self):
        cfg = config_mod.load_config(str(CONFIG_PATH))
        maps = geometry.build_maps(cfg)
        out = geometry.ascii_map(cfg, maps)
        self.assertIn("kick-left", out)
        self.assertIn("+", out)


class DdpTest(unittest.TestCase):
    def test_header_layout(self):
        pkt = ddp.ddp_packet(seq=5, offset=0x0102, payload=b"\x01\x02\x03",
                             push=True)
        self.assertEqual(pkt[0], 0x41)                    # v1 | push
        self.assertEqual(pkt[1], 5)
        self.assertEqual(pkt[2], ddp.DDP_TYPE_RGB8)
        self.assertEqual(pkt[3], 1)
        self.assertEqual(pkt[4:8], b"\x00\x00\x01\x02")   # offset BE
        self.assertEqual(pkt[8:10], b"\x00\x03")          # len BE
        self.assertEqual(pkt[10:], b"\x01\x02\x03")

    def test_no_push(self):
        pkt = ddp.ddp_packet(seq=1, offset=0, payload=b"", push=False)
        self.assertEqual(pkt[0], 0x40)

    def test_sequence_is_per_node(self):
        import socket
        rx = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
        for r in rx:
            r.bind(("127.0.0.1", 0))
            r.settimeout(1)
        s = ddp.DDPSender()
        for _ in range(17):                    # wraps 15 -> 1
            for r in rx:
                s.send_frame("127.0.0.1", r.getsockname()[1], b"\x00" * 3)
        for r in rx:
            seqs = [r.recvfrom(64)[0][1] for _ in range(17)]
            self.assertEqual(seqs, list(range(1, 16)) + [1, 2])
        s.close()

    def test_multi_packet_frame(self):
        # a 9 m daisy chain is 540 LEDs = 1620 B > one packet; the splitter
        # must emit offset-continued packets with push only on the last
        sent = []

        class FakeSock:
            def sendto(self, pkt, addr):
                sent.append(pkt)

            def close(self):
                pass

        sender = ddp.DDPSender()
        sender._sock = FakeSock()
        payload = bytes(range(256)) * 6 + bytes(84)   # 1620 B
        sender.send_frame("host", 4048, payload)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0], 0x40)                       # no push
        self.assertEqual(sent[1][0], 0x41)                       # push
        self.assertEqual(sent[0][1], sent[1][1])                 # same seq
        self.assertEqual(sent[0][4:8], (0).to_bytes(4, "big"))
        self.assertEqual(sent[0][8:10], (1440).to_bytes(2, "big"))
        self.assertEqual(sent[1][4:8], (1440).to_bytes(4, "big"))
        self.assertEqual(sent[1][8:10], (180).to_bytes(2, "big"))
        self.assertEqual(sent[0][10:] + sent[1][10:], payload)   # reassembles


def make_cfg(**render_over):
    d = {
        "nodes": [{
            "name": "bench", "host": "127.0.0.1", "num_leds": 60,
            "budget_ma": 2000, "waypoints": [[0, 0, 0], [59, 2950, 0]],
        }],
        "render": render_over,
        "room": {"polygon": [[0, -500], [3000, -500], [3000, 1500], [0, 1500]]},
    }
    d = config_mod._merge(config_mod.DEFAULTS, d)
    d["nodes"] = [config_mod._merge(config_mod.NODE_DEFAULTS, n)
                  for n in d["nodes"]]
    config_mod._validate(d)
    return config_mod.Cfg(d)


class RendererTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(output_ema_s=0.0001)  # effectively no output lag
        self.params = config_mod.Params(self.cfg)
        self.maps = geometry.build_maps(self.cfg)
        self.r = Renderer(self.cfg, self.params, self.maps)
        self.r.master = 1.0

    def rgb(self, frames):
        return np.frombuffer(frames["bench"], dtype=np.uint8).reshape(-1, 3)

    def settle(self, targets, occupied=True, steps=40):
        for _ in range(steps):
            frames = self.r.render(1 / 30, targets, occupied)
        return frames

    def test_pool_peaks_at_target(self):
        # target over LED 30 (x = 30/59*2950 ≈ 1500)
        frames = self.settle([(1500.0, 0.0, 1.0)])
        rgb = self.rgb(frames)
        bright = rgb.sum(axis=1)
        self.assertEqual(int(np.argmax(bright)), 30)
        # gaussian falloff: at 1 sigma from centre, dimmer but lit
        self.assertGreater(bright[30], bright[20])
        self.assertGreater(bright[20], bright[10])

    def test_ambient_floor_when_occupied(self):
        frames = self.settle([])
        self.assertGreater(self.rgb(frames).sum(), 0)

    def test_dark_when_unoccupied(self):
        self.r.master = 0.0
        frames = self.settle([], occupied=False, steps=5)
        self.assertEqual(self.rgb(frames).sum(), 0)

    def test_confidence_scales_pool(self):
        full = self.rgb(self.settle([(1500.0, 0.0, 1.0)])).astype(int)
        half = self.rgb(self.settle([(1500.0, 0.0, 0.3)])).astype(int)
        self.assertLess(half[30].sum(), full[30].sum())

    def test_budget_clamp_scales_whole_frame(self):
        # full-white manual overload vs the 2000 mA budget
        self.params.mode = "manual"
        self.params.manual_on = True
        self.params.manual_rgb = (255, 255, 255)
        self.params.manual_brightness = 255
        frames = self.settle([], steps=60)
        ns = self.r.nodes["bench"]
        self.assertTrue(ns.clamped)
        self.assertLessEqual(ns.est_ma, 2000 + 1)
        rgb = self.rgb(frames)
        # scaled uniformly, not per-pixel: all LEDs identical
        self.assertTrue((rgb == rgb[0]).all())
        self.assertLess(int(rgb[0][0]), 255)

    def test_unclamped_estimate_sane(self):
        # one pool at defaults should sit way under the per-side budget,
        # backing the §3.1 "realistic" row
        self.settle([(1500.0, 0.0, 1.0)])
        ns = self.r.nodes["bench"]
        self.assertFalse(ns.clamped)
        self.assertLess(ns.est_ma, 1500)

    def test_disabled_goes_dark(self):
        self.params.enabled = False
        frames = self.settle([(1500.0, 0.0, 1.0)], steps=200)
        self.assertEqual(self.rgb(frames).sum(), 0)

    def test_wire_order_respected(self):
        cfg = make_cfg(output_ema_s=0.0001, warm_rgb=[200, 100, 10])
        cfg.raw()["nodes"][0]["color_order"] = "GRB"
        params = config_mod.Params(cfg)
        params.mode = "manual"
        params.manual_on = True
        params.manual_rgb = (200, 100, 10)
        params.manual_brightness = 255
        r = Renderer(cfg, params, geometry.build_maps(cfg))
        r.master = 1.0
        for _ in range(40):
            frames = r.render(1 / 30, [], True)
        first = np.frombuffer(frames["bench"], dtype=np.uint8)[:3]
        self.assertEqual(list(first), [100, 200, 10])   # G, R, B on the wire


class TrackerTest(unittest.TestCase):
    POLY = [(0, 0), (4000, 0), (4000, 2000), (0, 2000)]

    def make(self, zones=(), **over):
        d = dict(config_mod.DEFAULTS["tracker"])
        d.update(over)
        cfg = config_mod.Cfg(d)
        params = config_mod.Params(config_mod.Cfg(config_mod.DEFAULTS))
        return tracker_mod.Tracker(cfg, params, self.POLY, zones)

    def step(self, tr, t0, seconds, dets, hz=10):
        t = t0
        for _ in range(int(seconds * hz)):
            t += 1.0 / hz
            tr.update(t, dets)
        return t

    def test_point_in_polygon(self):
        self.assertTrue(tracker_mod.point_in_polygon(1000, 1000, self.POLY))
        self.assertFalse(tracker_mod.point_in_polygon(-10, 1000, self.POLY))
        self.assertFalse(tracker_mod.point_in_polygon(1000, 2500, self.POLY))

    def test_confidence_ramps_and_decays(self):
        tr = self.make()
        t = self.step(tr, 0.0, 1.0, [(1000, 1000, 20)])
        self.assertEqual(len(tr.tracks), 1)
        self.assertEqual(tr.tracks[0].confidence, 1.0)
        # moving target that vanishes: gone after ~decay_s (2 s), not sooner
        t2 = self.step(tr, t, 1.0, [])
        self.assertTrue(tr.tracks and tr.tracks[0].confidence < 1.0)
        self.step(tr, t2, 1.5, [])
        self.assertEqual(tr.tracks, [])

    def test_identity_survives_movement(self):
        tr = self.make()
        t = self.step(tr, 0.0, 0.5, [(1000, 1000, 30)])
        tid = tr.tracks[0].id
        for i in range(20):                   # walk 1 m in 2 s
            t += 0.1
            tr.update(t, [(1000 + i * 50, 1000, 30)])
        self.assertEqual(len(tr.tracks), 1)
        self.assertEqual(tr.tracks[0].id, tid)

    def test_two_targets_keep_identity(self):
        tr = self.make()
        t = 0.0
        for i in range(20):
            t += 0.1
            tr.update(t, [(500 + i * 10, 500, 20), (3000 - i * 10, 1500, 20)])
        self.assertEqual(len(tr.tracks), 2)
        ids = sorted((round(tr.tracks[0].x / 100), round(tr.tracks[1].x / 100)))
        self.assertEqual(ids, [7, 28])        # ~700 and ~2810 mm

    def test_outside_polygon_rejected(self):
        tr = self.make()
        self.step(tr, 0.0, 1.0, [(5000, 1000, 20)])   # through the doorway
        self.assertEqual(tr.tracks, [])

    def test_ghost_zone_blocks_birth_not_transit(self):
        zone = [(900, 900), (1100, 900), (1100, 1100), (900, 1100)]
        tr = self.make(zones=[zone])
        self.step(tr, 0.0, 1.0, [(1000, 1000, 20)])   # born inside: ignored
        self.assertEqual(tr.tracks, [])
        # born outside, walks in: keeps tracking
        t = self.step(tr, 10.0, 0.5, [(600, 1000, 20)])
        self.assertEqual(len(tr.tracks), 1)
        self.step(tr, t, 0.5, [(1000, 1000, 20)])
        self.assertEqual(len(tr.tracks), 1)
        self.assertGreater(tr.tracks[0].confidence, 0.5)

    def test_static_hold_outlasts_normal_decay(self):
        tr = self.make()
        t_static = self.step(tr, 0.0, 1.0, [(1000, 1000, 0)])   # stationary
        tr2 = self.make()
        t_moving = self.step(tr2, 0.0, 1.0, [(1000, 1000, 50)])  # moving
        self.step(tr, t_static, 4.0, [])
        self.step(tr2, t_moving, 4.0, [])
        self.assertEqual(len(tr.tracks), 1, "static target should be held")
        self.assertTrue(tr.tracks[0].held)
        self.assertEqual(tr2.tracks, [], "moving target should have decayed")

    def test_smoothing_lags_raw(self):
        tr = self.make()
        t = self.step(tr, 0.0, 1.0, [(1000, 1000, 20)])
        tr.update(t + 0.1, [(1500, 1000, 20)])        # 0.5 m jump
        tk = tr.tracks[0]
        self.assertEqual(tk.raw_x, 1500)
        self.assertLess(tk.x, 1400)                   # EMA lags the jump
        self.assertGreater(tk.x, 1000)

    def test_occupied_honours_idle_timeout(self):
        tr = self.make()
        t = self.step(tr, 0.0, 1.0, [(1000, 1000, 50)])
        t = self.step(tr, t, 3.0, [])                 # track fully decayed
        self.assertEqual(tr.tracks, [])
        self.assertTrue(tr.occupied(t, 30.0))
        self.assertFalse(tr.occupied(t + 31.0, 30.0))


class SimAppTest(unittest.TestCase):
    """Only runs where fastapi is installed (it is not a test dependency)."""

    def test_ws_endpoint_binds_the_websocket_param(self):
        try:
            from kickboard.sim import create_app
        except ImportError:
            self.skipTest("fastapi not installed")
        app = create_app(object())
        route = next(r for r in app.routes if getattr(r, "path", "") == "/ws")
        # Regression: with postponed annotations and a lazily imported
        # WebSocket type, FastAPI treated `sock` as a required query param
        # and refused every connection with HTTP 403.
        self.assertEqual(route.dependant.websocket_param_name, "sock")
        self.assertEqual([p.name for p in route.dependant.query_params], [])


class ConfigTest(unittest.TestCase):
    def test_repo_config_loads(self):
        cfg = config_mod.load_config(str(CONFIG_PATH))
        self.assertEqual(len(cfg.nodes), 2)
        self.assertEqual(cfg.nodes[0].name, "kick-left")
        self.assertEqual(int(cfg.render.sigma_mm), 500)

    def test_cct_to_rgb(self):
        r, g, b = config_mod.cct_to_rgb(2700)
        self.assertEqual(r, 255)
        self.assertGreater(g, b)          # warm: more green than blue
        r2, g2, b2 = config_mod.cct_to_rgb(6500)
        self.assertGreater(b2, b)         # cooler: more blue

    def test_mode_command_bytes(self):
        # PLAN.md §7 verbatim
        self.assertEqual(radar.CMD_MULTI_TARGET.hex(" "),
                         "fd fc fb fa 02 00 90 00 04 03 02 01")


if __name__ == "__main__":
    unittest.main()
