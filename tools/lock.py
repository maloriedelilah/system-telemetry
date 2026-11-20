#!/usr/bin/env python3
"""Write requirements.lock: exact pins with the sha256 of every file PyPI publishes for them (OPS-75).

    python3 tools/lock.py paho-mqtt==2.2.0 psutil==7.2.2

Every file (all wheels and the sdist) is hashed, so one lock serves Linux, Windows, ARM and every
Python the fleet runs. pip then installs with --require-hashes --no-deps: nothing that isn't pinned
here, and nothing whose bytes differ from what was locked. stdlib only.
"""
import json
import sys
import urllib.request

HEADER = """\
# Exact versions with every published file's sha256 (OPS-75): update.sh's finish (and install.sh,
# OPS-75 part B) install with --require-hashes --no-deps, so an unattended update never takes
# whatever PyPI serves that hour. requirements.txt keeps the loose ranges; regenerate this with
# tools/lock.py when a pin moves.
"""


def main(pins):
    out = [HEADER]
    for pin in pins:
        name, _, version = pin.partition("==")
        if not name or not version:
            sys.exit(f"pin as name==version, not {pin!r}")
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as r:
            files = sorted(json.load(r)["urls"], key=lambda u: u["filename"])
        if not files:
            sys.exit(f"PyPI lists no files for {pin}")
        hashes = " \\\n".join(f"    --hash=sha256:{u['digests']['sha256']}" for u in files)
        out.append(f"{name}=={version} \\\n{hashes}")
    with open("requirements.lock", "w") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:] or sys.exit(__doc__))
