#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retry_unmatched.py
====================

One-off helper for people upgrading from an older version of this repo.

`heroic_categorize.py` and `heroic_import_external.py` cache every Steam
lookup in `steam_cache.json`, keyed by the exact title string, so that
re-running never re-downloads what it already knows. That is normally exactly
what you want -- but it also means that once a title has been cached as
"no match", it stays that way forever, even after a matching improvement
(such as the one that now strips edition/version qualifiers before
comparing titles) that would have found it.

This script removes every "no match" entry (appid: null) from the cache, so
the next `scan` / `steam` / `favorites` run retries exactly those titles --
and only those, keeping every successful match untouched.

Usage:
    python heroic_import_external.py steam --list steam-games.txt        # old run, some titles failed
    python retry_unmatched.py                                            # clear the "no match" entries
    python heroic_import_external.py steam --list steam-games.txt        # retry, only the failed ones cost an API call
"""

import argparse
import json
import os
import shutil
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--cache", default="steam_cache.json", help="Cache file to clean up")
    args = parser.parse_args()

    if not os.path.isfile(args.cache):
        print(f"No cache file found at {args.cache} -- nothing to do.")
        return

    with open(args.cache, "r", encoding="utf-8") as f:
        cache = json.load(f)

    no_match = [title for title, entry in cache.items() if not entry.get("appid")]
    if not no_match:
        print("No cached 'no match' entries -- nothing to retry.")
        return

    print(f"{len(no_match)} title(s) will be retried on the next run:")
    for t in no_match:
        print(f"  - {t}")

    backup = args.cache + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.cache, backup)
    print(f"\nCache backed up to: {backup}")

    for title in no_match:
        del cache[title]

    with open(args.cache, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    print(f"{len(no_match)} entries removed from {args.cache}.")
    print("Re-run your scan/steam/favorites command: only these titles will")
    print("hit the Steam/SteamSpy API again, everything else stays cached.")


if __name__ == "__main__":
    main()
