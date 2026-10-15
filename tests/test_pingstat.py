"""ha/shell/pingstat.sh — parse both ping dialects into the JSON HA reads.

A stub `ping` (PINGSTAT_PING — busybox sh would run its own applet ahead
of one on PATH) replays captured output, so the test needs no network
and exercises the real script under the system sh/awk.
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "ha" / "shell" / "pingstat.sh"

BUSYBOX_OK = """\
PING 8.8.8.8 (8.8.8.8): 56 data bytes
64 bytes from 8.8.8.8: seq=0 ttl=117 time=12.000 ms
64 bytes from 8.8.8.8: seq=1 ttl=117 time=14.000 ms
64 bytes from 8.8.8.8: seq=2 ttl=117 time=11.000 ms
64 bytes from 8.8.8.8: seq=3 ttl=117 time=13.000 ms

--- 8.8.8.8 ping statistics ---
5 packets transmitted, 4 packets received, 20% packet loss
round-trip min/avg/max = 11.000/12.500/14.000 ms
"""

IPUTILS_OK = """\
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=58 time=9.80 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=58 time=10.2 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=58 time=10.4 ms (DUP!)

--- 1.1.1.1 ping statistics ---
2 packets transmitted, 2 received, +1 duplicates, 0% packet loss, time 1001ms
rtt min/avg/max/mdev = 9.800/10.000/10.200/0.200 ms
"""

ALL_LOST = """\
PING 8.8.8.8 (8.8.8.8): 56 data bytes

--- 8.8.8.8 ping statistics ---
10 packets transmitted, 0 packets received, 100% packet loss
"""

NO_ROUTE = "ping: sendto: Network is unreachable\n"
NO_PERM = "ping: permission denied (are you root?)\n"


class PingstatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, output, target="8.8.8.8", route=None):
        (self.dir / "out.txt").write_text(output)
        stub = self.dir / "ping"
        # Record the argv so the gateway test can see which host was pinged.
        stub.write_text(f'#!/bin/sh\necho "$@" > "{self.dir}/argv"\ncat "{self.dir}/out.txt"\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=f"{self.dir}:{os.environ['PATH']}",
                   PINGSTAT_COUNT="10", PINGSTAT_PING=str(stub))
        if route is not None:
            (self.dir / "route").write_text(route)
            env["PINGSTAT_ROUTE"] = str(self.dir / "route")
        res = subprocess.run(["sh", str(SCRIPT), target], env=env,
                             capture_output=True, text=True, check=True)
        lines = res.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, res.stdout)
        return json.loads(lines[0])

    def test_busybox_partial_loss(self):
        r = self.run_with(BUSYBOX_OK)
        self.assertEqual((r["sent"], r["recv"], r["loss"]), (5, 4, 20.0))
        self.assertEqual((r["rtt"], r["min"], r["max"]), (12.5, 11.0, 14.0))
        # |14-12| + |11-14| + |13-11| = 7 over 3 gaps
        self.assertAlmostEqual(r["jitter"], 2.3)
        self.assertIsNone(r["error"])

    def test_iputils_ignores_duplicates_and_summary_time(self):
        r = self.run_with(IPUTILS_OK, target="1.1.1.1")
        self.assertEqual((r["sent"], r["recv"], r["loss"]), (2, 2, 0.0))
        self.assertEqual(r["rtt"], 10.0)
        self.assertEqual(r["jitter"], 0.4)

    def test_total_loss_is_an_outage_not_an_error(self):
        r = self.run_with(ALL_LOST)
        self.assertEqual((r["sent"], r["recv"], r["loss"]), (10, 0, 100.0))
        self.assertIsNone(r["rtt"])
        self.assertIsNone(r["error"])

    def test_no_route_counts_as_total_loss(self):
        r = self.run_with(NO_ROUTE)
        self.assertEqual(r["loss"], 100.0)
        self.assertIsNone(r["error"])

    def test_broken_probe_is_an_error_not_an_outage(self):
        r = self.run_with(NO_PERM)
        self.assertIsNone(r["loss"])
        self.assertIn("permission denied", r["error"])

    def test_gateway_from_route_table(self):
        route = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "eth0\t0008A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
            "eth0\t00000000\t0108A8C0\t0003\t0\t0\t100\t00000000\n"
        )
        r = self.run_with(ALL_LOST, target="gw", route=route)
        self.assertEqual(r["target"], "192.168.8.1")
        self.assertIn("192.168.8.1", (self.dir / "argv").read_text())

    def test_gateway_missing_route_is_an_error(self):
        r = self.run_with(ALL_LOST, target="gw", route="Iface\tDestination\tGateway\n")
        self.assertEqual(r["error"], "no default route")
        self.assertIsNone(r["loss"])


if __name__ == "__main__":
    unittest.main()
