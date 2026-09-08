#!/usr/bin/env python3
"""Print log lines the C6 nodes send over UDP (strip_node LOG_HOST/LOG_PORT).

    python3 tools/nodelog.py            # listen on 4050, print with timestamps
    python3 tools/nodelog.py --port 4050 --grep watchdog
"""

import argparse
import re
import socket
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4050)
    ap.add_argument("--grep", help="only lines matching this regex")
    args = ap.parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    pat = re.compile(args.grep) if args.grep else None
    print(f"listening on udp/{args.port} (^C to stop)", flush=True)
    while True:
        data, (host, _) = sock.recvfrom(2048)
        line = data.decode(errors="replace").rstrip()
        if pat and not pat.search(line):
            continue
        print(f"{time.strftime('%H:%M:%S')} {host:15s} {line}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
