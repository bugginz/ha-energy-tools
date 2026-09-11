#!/usr/bin/env python3
"""Aldi Mobile SIM usage -> HA sensors.

The X2000's failover SIM is an Aldi $95 yearly data plan; the only way to see
what's left is the my.aldimobile.com.au portal. This logs in the same way the
browser does (Symfony form: GET / for the session cookie + CSRF token, POST
/login_check, re-GET the dashboard) and publishes what the dashboard shows:

    sensor.aldi_sim_data_remaining   GB, with days/expiry/plan attributes

Credentials live in /opt/stack/aldi/credentials.env (LOGIN= / PASSWORD=,
mode 600, NOT in the repo, created by hand). One login attempt per run and a
gentle cadence (systemd timer, 6h) — a telco portal is somebody else's
infrastructure and lockouts help nobody.

Runs on the Pi from cron-like timer; stdlib only.
"""
import html
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request

BASE = "https://my.aldimobile.com.au"
CREDS = "/opt/stack/aldi/credentials.env"
HA = "http://localhost:8123"
HA_TOKEN_FILE = "/opt/stack/energy_tools/data/.config/sen66/ha_token"
UA = "Mozilla/5.0 (X11; Linux aarch64) home-usage-check"


def die(msg):
    print("aldi_usage: " + msg, file=sys.stderr)
    sys.exit(1)


def load_creds():
    creds = {}
    try:
        for line in open(CREDS):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    except FileNotFoundError:
        die("no %s — create it with LOGIN= and PASSWORD= lines, chmod 600" % CREDS)
    if not creds.get("LOGIN") or not creds.get("PASSWORD"):
        die("credentials.env needs LOGIN= and PASSWORD=")
    return creds


def fetch(opener, path, data=None):
    req = urllib.request.Request(BASE + path,
                                 data=urllib.parse.urlencode(data).encode() if data else None,
                                 headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace"), r.geturl()


def main():
    creds = load_creds()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    page, _ = fetch(opener, "/")
    m = re.search(r'name="_csrf_token"\s+value="([^"]+)"', page)
    if not m:
        die("no CSRF token on the login page — portal layout changed?")

    page, url = fetch(opener, "/login_check", {
        "login_user[login]": creds["LOGIN"],
        "login_user[password]": creds["PASSWORD"],
        "_csrf_token": m.group(1),
    })
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    if "Plan data remaining" not in text:
        # some portals bounce to / after login — one follow-up GET
        page, url = fetch(opener, "/")
        text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    if "Plan data remaining" not in text:
        die("login did not reach the dashboard (landed on %s) — check credentials" % url)

    def grab(pattern):
        m = re.search(pattern, text)
        return m.group(1) if m else None

    remaining = grab(r"Plan data remaining\s*([\d.]+)\s*GB")
    total = grab(r"Total data for you to use\s*([\d.]+)\s*GB")
    days = grab(r"Days remaining\s*(\d+)")
    expiry = grab(r"Days remaining\s*\d+\s*\(Expiry\s*([\d/]+)\)")
    if remaining is None:
        die("dashboard reached but 'Plan data remaining' not parseable")

    tok = open(HA_TOKEN_FILE).read().strip()
    body = json.dumps({
        "state": remaining,
        "attributes": {
            "unit_of_measurement": "GB",
            "friendly_name": "Aldi SIM data remaining",
            "icon": "mdi:sim",
            "total_gb": float(total) if total else None,
            "days_remaining": int(days) if days else None,
            "expiry": expiry,
        },
    }).encode()
    req = urllib.request.Request(HA + "/api/states/sensor.aldi_sim_data_remaining",
                                 data=body, method="POST",
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()
    print("aldi_usage: %s GB remaining, %s days to expiry" % (remaining, days))


if __name__ == "__main__":
    main()
