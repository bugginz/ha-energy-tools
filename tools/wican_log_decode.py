#!/usr/bin/env python3
"""Decode Car Scanner (ELM327) adapter logs into per-ECU/per-DID value histories.

Car Scanner's "Save log" output is a wall of ELM chatter: commands, `>` prompts, CAN
frames with 29-bit headers (`18DAF144 10 09 62 A0 10 ...`), and `\\r`-separated
lines inside each exchange. This tool re-assembles the ISO-TP frames per responding
ECU and prints, for every (ECU, DID) pair, the ordered run-length history of the
payload — which is what you stare at to work out what a byte means.

    python3 tools/wican_log_decode.py log3.txt                # summary of every DID
    python3 tools/wican_log_decode.py log3.txt --did A009     # one DID, every read
    python3 tools/wican_log_decode.py log3.txt --decode       # known Fiat 500e fields
    python3 tools/wican_log_decode.py --wican-index 62A0091A420FE60FD6000B8010800F

`--wican-index` prints, for each data byte of a UDS response, the WiCAN `B<n>`
index to use in an Automate/custom-PID expression. WiCAN indexes the RAW CAN
frames including every ISO-TP PCI byte (B0 = first PCI byte, and each continuation
frame's `2x` byte also takes an index) — see
https://github.com/meatpiHQ/wican-fw/discussions/663. A 16-bit value that straddles
a frame boundary therefore cannot use `[Bx:By]`; write `Bx*256+By` instead.

Only the read-by-DID (0x22 / 0x62) traffic is decoded; mode 01/09 replies are
listed raw. Known field decodes live in KNOWN below and are documented in
docs/wican/fiat500e-2020-decoded-pids.md.
"""
import argparse
import collections
import itertools
import re
import sys

CANID = re.compile(r"^18DAF1([0-9A-F]{2})([0-9A-F]+)$")
HDR = re.compile(r"ATSHDA([0-9A-F]{2})F1")


def parse(text):
    """Yield (ecu, request, payload_hex) for every reassembled response in the log."""
    # Car Scanner writes one exchange per '>' prompt; inside it, lines are '\r'.
    for exch in text.split(">"):
        lines = [l.strip() for l in exch.replace("\r\n", "\r").split("\r") if l.strip()]
        req = None
        frames = collections.OrderedDict()
        for l in lines:
            if HDR.match(l):
                continue
            if l.startswith(("AT", "ST", "[", "SEARCHING")) or l in ("OK", "NO DATA", "?"):
                continue
            m = CANID.match(l)
            if m:
                frames.setdefault(m.group(1), []).append(m.group(2))
                continue
            if req is None and re.match(r"^[0-9A-F]+$", l):
                req = l
                # Car Scanner appends the expected frame count: 22A0102 -> 22A010
                if len(req) == 7 and req.startswith("22"):
                    req = req[:6]
                if len(req) == 3 and req[0] in "013":
                    req = req[:2]
        for ecu, fr in frames.items():
            payload = ""
            for f in fr:
                pci = int(f[0], 16)
                if pci == 0:                      # single frame: 0L dd..
                    payload += f[2:2 + 2 * int(f[1], 16)]
                elif pci == 1:                    # first frame: 1LLL dd..
                    payload += f[4:]
                elif pci == 2:                    # consecutive: 2N dd..
                    payload += f[2:]
            if payload:
                yield ecu, req, payload


def wican_index(k, total_len):
    """WiCAN B-index of UDS data byte k (0-based, after SID+DID) for a response of
    total_len bytes (SID + DID + data)."""
    if total_len <= 7:            # single frame: PCI, SID, DID, DID, data...
        return 4 + k
    if k < 3:                     # first frame: PCI, LEN, SID, DID, DID, d0, d1, d2
        return 5 + k
    m = k - 3                     # consecutive frames: PCI + 7 data bytes each
    n = m // 7 + 1
    return 8 * n + 1 + m % 7


def spaced(h):
    return " ".join(h[i:i + 2] for i in range(0, len(h), 2))


def u16(d, i):
    return d[i] * 256 + d[i + 1]


def u24(d, i):
    return d[i] * 65536 + d[i + 1] * 256 + d[i + 2]


# Known Fiat 500e (2020+) field decodes. Each entry: (ecu, did) -> fn(data_bytes) -> dict
KNOWN = {
    ("44", "A010"): lambda d: {
        "soc_min_cell_%": d[0] / 2.55, "soc_max_cell_%": d[1] / 2.55,
        "soc_%_(d2)": d[2] / 2.55, "soc_real_%_(d3=B9)": d[3] / 2.55,
        "soc_%_(d4)": d[4] / 2.55, "soc_%_(d5)": d[5] / 2.55},
    ("44", "A011"): lambda d: {"hv_pack_V": u16(d, 0) / 10, "hv_V_b": u16(d, 2) / 10,
                               "hv_V_c": u16(d, 4) / 10, "hv_V_d": u16(d, 22) / 10},
    ("44", "A009"): lambda d: {"cell_max_idx": d[0] + 1, "cell_min_idx": d[1] + 1,
                               "cell_max_mV": u16(d, 2), "cell_min_mV": u16(d, 4),
                               "temp_max_C": u16(d, 8) - 32768, "temp_min_C": u16(d, 10) - 32768},
    ("44", "A021"): lambda d: {"temp_max_C": u16(d, 0) - 32768, "temp_min_C": u16(d, 2) - 32768,
                               "temp_3_C": u16(d, 4) - 32768},
    ("44", "A029"): lambda d: {"soh_like_%": u16(d, 0) / 655.35, "cap_nominal_Ah": u16(d, 3) / 10,
                               "cap_now_Ah": u16(d, 5) / 10,
                               "soh_cap_%": u16(d, 5) / u16(d, 3) * 100},
    ("44", "A001"): lambda d: {"f0_counter": u24(d, 0) / 10, "f1_Ah": u24(d, 3) / 10,
                               "f2_Ah": u24(d, 6) / 10, "f3_kWh": u24(d, 9) / 10,
                               "f4_kWh": u24(d, 12) / 10},
    ("44", "A200"): lambda d: {"module_temps_C": [b - 40 for b in d[:18]]},
    ("42", "3062"): lambda d: {"hv_V": u16(d, 0) / 50},
    ("42", "B562"): lambda d: {"hv_V": u16(d, 0) / 50},
    ("42", "4052"): lambda d: {"hv_bus_V": u16(d, 4)},
    ("42", "3027"): lambda d: {"odo_km": u24(d, 0) / 10},
    ("42", "3065"): lambda d: {"temps_C": [None if b == 0x28 else b - 40 for b in d]},
    ("42", "4053"): lambda d: {"temp_C": d[1] - 40},
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="*", help="Car Scanner log file(s)")
    ap.add_argument("--did", help="only this DID (e.g. A009); prints every read in order")
    ap.add_argument("--ecu", help="only this responding ECU (e.g. 44)")
    ap.add_argument("--decode", action="store_true", help="apply KNOWN decodes and print values")
    ap.add_argument("--runs", type=int, default=25, help="max runs to print per DID (summary mode)")
    ap.add_argument("--wican-index", metavar="HEX", help="print WiCAN B-indices for a UDS response payload")
    a = ap.parse_args()

    if a.wican_index:
        h = re.sub(r"[^0-9A-Fa-f]", "", a.wican_index).upper()
        d = bytes.fromhex(h)
        total = len(d)
        print(f"payload {spaced(h)}  ({total} bytes -> {'single' if total <= 7 else 'multi'}-frame)")
        print("  SID/DID:", spaced(h[:6]))
        for k, b in enumerate(d[3:]):
            print(f"  data[{k:2d}] = {b:02X}  ->  B{wican_index(k, total)}")
        return

    if not a.logs:
        ap.error("give at least one log file (or --wican-index)")
    pairs = []
    for path in a.logs:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            pairs.extend(parse(f.read()))

    by = collections.OrderedDict()
    for ecu, req, pl in pairs:
        if not pl.startswith("62") or len(pl) < 6:
            key = (ecu, "raw:" + (req or "?"))
            by.setdefault(key, []).append(pl)
            continue
        by.setdefault((ecu, pl[2:6]), []).append(pl[6:])

    for (ecu, did), seq in sorted(by.items()):
        if a.ecu and ecu != a.ecu:
            continue
        if a.did and did != a.did.upper():
            continue
        runs = [(k, len(list(g))) for k, g in itertools.groupby(seq)]
        print(f"\n=== ECU {ecu}  DID {did}: {len(seq)} reads, {len(set(seq))} distinct, {len(runs)} runs")
        show = runs if a.did else runs[:a.runs]
        for k, n in show:
            line = f"  {n:4d}x {spaced(k)}"
            if a.decode and (ecu, did) in KNOWN:
                try:
                    dec = KNOWN[(ecu, did)](bytes.fromhex(k))
                    line += "   => " + ", ".join(
                        f"{n2}={v:.1f}" if isinstance(v, float) else f"{n2}={v}" for n2, v in dec.items())
                except Exception as e:  # short/odd payload
                    line += f"   (decode failed: {e})"
            print(line)
        if len(show) < len(runs):
            print(f"  ... {len(runs) - len(show)} more runs (use --did {did} for all)")


if __name__ == "__main__":
    main()
