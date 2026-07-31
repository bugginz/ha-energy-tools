#!/usr/bin/env python3
"""Install a HACS frontend repository over the websocket API.

HACS has no REST surface — everything goes through websocket commands. This finds a
repository by full_name, downloads it, and reports the resulting resource URL so it can be
registered with Lovelace.

    HA_TOKEN=... python3 hacs_install.py find lovelace-card-mod
    HA_TOKEN=... python3 hacs_install.py install thomasloven/lovelace-card-mod
"""
import sys

from ha_dashboard import HAWS


def repos(ha):
    for cmd in ("hacs/repositories/list", "hacs/repositories"):
        try:
            r = ha.cmd(type=cmd)
            if r:
                return r
        except SystemExit:
            continue
    raise SystemExit("could not list HACS repositories")


def main(argv):
    action, term = argv[0], argv[1]
    ha = HAWS()
    try:
        found = [r for r in repos(ha)
                 if term.lower() in str(r.get("full_name", "")).lower()]
        if action == "find":
            for r in found:
                print(f"{r.get('id')}\t{r.get('full_name')}\t{r.get('category')}\t"
                      f"installed={r.get('installed')}\tver={r.get('installed_version')}")
            return
        if action == "install":
            if not found:
                raise SystemExit(f"no HACS repository matching {term!r}")
            r = found[0]
            if r.get("installed"):
                print(f"already installed: {r['full_name']} {r.get('installed_version')}")
            else:
                ha.cmd(type="hacs/repository/download", repository=str(r["id"]))
                print(f"downloaded {r['full_name']}")
            name = r["full_name"].split("/")[-1]
            print(f"resource url: /hacsfiles/{name}/{name.replace('lovelace-', '')}.js")
            return
        raise SystemExit(f"unknown action {action}")
    finally:
        ha.close()


if __name__ == "__main__":
    main(sys.argv[1:])
