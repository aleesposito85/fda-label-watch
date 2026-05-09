"""Helper for adding new drugs.

Usage:
    python scripts/bootstrap_setids.py "ozempic" "humira" ...

For each drug name, queries DailyMed and prints up to 5 candidate setids in a
ready-to-paste drugs.yaml block. Pick the one whose manufacturer/label matches
what you actually want to track.
"""
from __future__ import annotations

import sys
import time
import urllib.parse

import requests

BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"


def search(name: str) -> list[dict]:
    url = f"{BASE}/spls.json?drug_name={urllib.parse.quote(name)}&pagesize=10"
    r = requests.get(url, timeout=20, headers={"User-Agent": "fda-label-watch/0.1"})
    r.raise_for_status()
    return r.json().get("data", [])


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    for q in sys.argv[1:]:
        results = search(q)
        if not results:
            print(f"# no results for {q!r}")
            continue
        print(f"# --- {q} ---")
        # Sort by spl_version descending: most active labels first.
        for r in sorted(results, key=lambda x: -x.get("spl_version", 0))[:5]:
            print(f"# {r['title']}  (v{r['spl_version']}, {r['published_date']})")
            print(f"#   - slug: {q.lower().replace(' ', '-')}")
            print(f"#     name: {q}")
            print(f"#     setid: {r['setid']}")
            print()
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
