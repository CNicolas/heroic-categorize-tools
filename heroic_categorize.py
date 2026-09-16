#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
heroic_categorize.py
=====================

Automatically categorises your Heroic Games Launcher (2.x) library without
IGDB: genres and tags come from the public Steam Store API (no key, no
account) and then from SteamSpy (no key, no account), which between them
cover nearly every PC game -- even the ones you bought on Epic/GOG/Amazon.

The workflow is deliberately split in two steps so that nothing is ever
written to your configuration without you reviewing it first:

  1) scan   -> reads the Heroic library, queries Steam/SteamSpy, proposes a
               category per game, writes "proposal.csv"
  2) apply  -> reads "proposal.csv" back (after you have optionally fixed it
               by hand in Excel/LibreOffice) and merges the result into
               store/config.json (with an automatic backup beforehand)

Usage:
    python heroic_categorize.py scan
    python heroic_categorize.py apply proposal.csv
    python heroic_categorize.py combos proposal.csv
    python heroic_categorize.py similar --anchor "Aven Colony" --category "Like Aven Colony"

Handy options:
    --heroic-dir <path>     Heroic config folder (auto-detected by default)
    --lang english          Language requested from Steam (default: english)
    --only-uncategorized    (scan) skip games that already have a category
    --limit N               (scan) cap the number of games processed (testing)

Runs on Linux, Windows and macOS. No external dependencies: Python 3 standard
library only.
"""

import argparse
import csv
import itertools
import json
import os
import platform
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

STEAM_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STEAMSPY_URL = "https://steamspy.com/api.php"
USER_AGENT = "Mozilla/5.0 (heroic-categorize-script)"

# ----------------------------------------------------------------------------
# Locating the Heroic configuration folder
# ----------------------------------------------------------------------------


def default_heroic_dir() -> str:
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "heroic")
    elif system == "Darwin":
        home = os.path.expanduser("~")
        return os.path.join(home, "Library", "Application Support", "heroic")
    else:
        home = os.path.expanduser("~")
        return os.path.join(home, ".config", "heroic")
    return os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "heroic")


# ----------------------------------------------------------------------------
# Reading the Heroic library (the launcher's own cache files)
# ----------------------------------------------------------------------------

# (path relative to the heroic folder, JSON key holding the games array)
LIBRARY_SOURCES = [
    (os.path.join("store_cache", "legendary_library.json"), "library"),
    (os.path.join("store_cache", "gog_library.json"), "games"),
    (os.path.join("store_cache", "nile_library.json"), "library"),
    (os.path.join("sideload_apps", "library.json"), "games"),
]


def load_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def collect_library(heroic_dir):
    """Return a deduplicated list of dicts {app_name, title, runner, heroic_id}.

    heroic_id is the identifier Heroic actually uses inside customCategories,
    formatted as '<app_name>_<runner>' (e.g.
    'dc07b9ead8214591b8df6d2546d2a0e3_legendary')."""
    games = {}
    for rel_path, key in LIBRARY_SOURCES:
        full_path = os.path.join(heroic_dir, rel_path)
        data = load_json(full_path)
        if data is None:
            print(f"  (info) file missing, skipped: {full_path}")
            continue
        entries = data.get(key, [])
        if not isinstance(entries, list):
            continue
        for g in entries:
            app_name = g.get("app_name")
            title = g.get("title")
            runner = g.get("runner", "?")
            if not app_name or not title:
                continue
            heroic_id = f"{app_name}_{runner}"
            games[heroic_id] = {
                "app_name": app_name,
                "title": title,
                "runner": runner,
                "heroic_id": heroic_id,
            }
    return list(games.values())


def load_existing_categories(config_path):
    data = load_json(config_path) or {}
    return data.get("games", {}).get("customCategories", {})


def already_categorized_appnames(custom_categories):
    seen = set()
    for app_names in custom_categories.values():
        seen.update(app_names)
    return seen


# ----------------------------------------------------------------------------
# Fetching genres/tags from Steam + SteamSpy (no account required)
# ----------------------------------------------------------------------------


def normalize(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def similarity(a, b):
    # Simple similarity; difflib is part of the standard library so we may as
    # well use it rather than rolling our own.
    import difflib

    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def http_get_json(url, params, timeout=15):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print(f"  (warning) request to {url} failed: {e}")
        return None


def steam_find_appid(title, lang, cc):
    data = http_get_json(
        STEAM_SEARCH_URL, {"term": title, "l": lang, "cc": cc}
    )
    if not data or not data.get("items"):
        return None
    best = None
    best_score = 0.0
    for item in data["items"][:5]:
        score = similarity(title, item.get("name", ""))
        if score > best_score:
            best_score = score
            best = item
    if best and best_score >= 0.6:
        return best["id"], best_score
    return None


def steamspy_get_tags_and_genre(appid):
    data = http_get_json(STEAMSPY_URL, {"request": "appdetails", "appid": appid})
    if not data:
        return [], ""
    tags = data.get("tags") or {}
    # tags is a dict {"Tag": votes}; sort by descending vote count
    sorted_tags = sorted(tags.items(), key=lambda kv: kv[1], reverse=True)
    tag_names = [normalize(t[0]) for t in sorted_tags]
    genre = data.get("genre") or ""
    return tag_names, normalize(genre)


def load_mapping(mapping_path):
    """Load mapping.json. Each entry may be either a single category (string)
    or a list of categories (e.g. "survival horror": ["Horror", "Survival"]).
    Internally everything is normalised to a list."""
    with open(mapping_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    normalized = {}
    for k, v in raw.items():
        if k == "_comment":
            continue
        if isinstance(v, list):
            normalized[normalize(k)] = list(v)
        else:
            normalized[normalize(k)] = [v]
    return normalized


def pick_categories(tag_names, genre_str, mapping, max_categories=3):
    """Return an ordered, duplicate-free list of matching categories.

    Community tags are walked first (most-voted to least-voted), then the
    official Steam genres, keeping every distinct category encountered up to
    max_categories. A single game can therefore end up in both "City Builder"
    and "Simulation" if it carries both tags."""
    found = []
    matched_on = []

    def try_add(key):
        if key not in mapping:
            return
        for cat in mapping[key]:
            if len(found) >= max_categories:
                return
            if cat not in found:
                found.append(cat)
                matched_on.append(key)

    for tag in tag_names:
        if len(found) >= max_categories:
            break
        try_add(tag)

    if len(found) < max_categories:
        for part in genre_str.split(","):
            if len(found) >= max_categories:
                break
            try_add(part.strip())

    return found, matched_on


# ----------------------------------------------------------------------------
# Command: scan
# ----------------------------------------------------------------------------


def cmd_scan(args):
    heroic_dir = args.heroic_dir
    config_path = os.path.join(heroic_dir, "store", "config.json")

    print(f"Using Heroic folder: {heroic_dir}")
    if not os.path.isdir(heroic_dir):
        print("That folder does not exist. Point at it with --heroic-dir.")
        sys.exit(1)

    mapping = load_mapping(args.mapping)
    print(f"{len(mapping)} tag/genre -> category rules loaded from {args.mapping}")

    print("Reading the Heroic library...")
    games = collect_library(heroic_dir)
    print(f"{len(games)} games found in the library.")

    existing = load_existing_categories(config_path)
    already = already_categorized_appnames(existing)
    if args.only_uncategorized:
        before = len(games)
        games = [g for g in games if g["heroic_id"] not in already]
        print(f"{before - len(games)} already-categorised games skipped (--only-uncategorized).")

    if args.limit:
        games = games[: args.limit]
        print(f"Capped at {len(games)} games for this run.")

    cache = {}
    if os.path.isfile(args.cache):
        cache = load_json(args.cache) or {}

    rows = []
    total = len(games)
    for i, game in enumerate(games, 1):
        title = game["title"]
        print(f"[{i}/{total}] {title}", end=" ... ")

        if title in cache:
            entry = cache[title]
            print("(cached)")
        else:
            found = steam_find_appid(title, args.lang, args.cc)
            if not found:
                entry = {"appid": None, "tags": [], "genre": ""}
                print("no Steam match")
            else:
                appid, score = found
                tags, genre = steamspy_get_tags_and_genre(appid)
                entry = {"appid": appid, "tags": tags, "genre": genre}
                print(f"appid={appid} (score {score:.2f})")
                time.sleep(args.delay)  # be gentle with the SteamSpy API
            cache[title] = entry
            # save the cache as we go so an interrupted scan can be resumed
            with open(args.cache, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)

        categories, matched_on = pick_categories(entry["tags"], entry["genre"], mapping, args.max_categories)
        rows.append(
            {
                "title": title,
                "app_name": game["app_name"],
                "runner": game["runner"],
                "heroic_id": game["heroic_id"],
                "category": "; ".join(categories),
                "matched_on": "; ".join(matched_on),
                "top_tags": ", ".join(entry["tags"][:6]),
            }
        )

    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["title", "app_name", "runner", "heroic_id", "category", "matched_on", "top_tags"],
        )
        writer.writeheader()
        writer.writerows(rows)

    matched = sum(1 for r in rows if r["category"])
    print()
    print(f"Done. {matched}/{len(rows)} games received an automatic category.")
    print(f"Proposal written to: {args.output}")
    print("Open that file (Excel/LibreOffice), fix or complete the 'category'")
    print("column if needed, then run:")
    print(f"    python heroic_categorize.py apply {args.output}")


# ----------------------------------------------------------------------------
# Command: combos (AND categories = intersections of existing categories)
# ----------------------------------------------------------------------------


def cmd_combos(args):
    with open(args.proposal, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # categories per row (already split on ";")
    row_categories = []
    for row in rows:
        cats = [c.strip() for c in (row.get("category") or "").split(";") if c.strip()]
        row_categories.append(cats)

    # for each pair of categories, count how many games carry BOTH
    combo_counts = {}
    for cats in row_categories:
        if len(cats) < 2:
            continue
        for a, b in itertools.combinations(sorted(set(cats)), 2):
            combo_counts[(a, b)] = combo_counts.get((a, b), 0) + 1

    kept_combos = {pair for pair, count in combo_counts.items() if count >= args.min_count}

    if not kept_combos:
        print(f"No combination reaches the threshold of {args.min_count} games. Nothing to do.")
        print("Try a lower --min-count if you expected some.")
        return

    print(f"{len(kept_combos)} combination(s) kept (>= {args.min_count} games):")
    for a, b in sorted(kept_combos):
        print(f"  {args.prefix}{a}+{b}  ({combo_counts[(a, b)]} games)")

    added = 0
    for row, cats in zip(rows, row_categories):
        cat_set = set(cats)
        extra = []
        for a, b in kept_combos:
            if a in cat_set and b in cat_set:
                combo_name = f"{args.prefix}{a}+{b}"
                if combo_name not in cat_set:
                    extra.append(combo_name)
        if extra:
            all_cats = cats + extra
            row["category"] = "; ".join(all_cats)
            added += len(extra)

    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print()
    print(f"{added} combined-category membership(s) added.")
    print(f"File written: {args.output}")
    print(f"Review it, then: python heroic_categorize.py apply {args.output}")


# ----------------------------------------------------------------------------
# Command: similar ("games like X", via tag similarity and/or filters)
# ----------------------------------------------------------------------------


def find_anchor_entry(anchor_title, cache):
    """Find the cache entry matching the given title (exact case-insensitive
    match first, then the closest one by similarity)."""
    norm_anchor = normalize(anchor_title)
    for title, entry in cache.items():
        if normalize(title) == norm_anchor:
            return title, entry
    best_title = None
    best_score = 0.0
    for title in cache:
        score = similarity(anchor_title, title)
        if score > best_score:
            best_score = score
            best_title = title
    if best_title and best_score >= 0.5:
        return best_title, cache[best_title]
    return None, None


def cmd_similar(args):
    heroic_dir = args.heroic_dir
    cache = load_json(args.cache) or {}
    if not cache:
        print(f"Cache missing or empty: {args.cache}")
        print("Run 'scan' first to populate the Steam/SteamSpy cache.")
        sys.exit(1)

    require = [normalize(t) for t in (args.require or "").split(",") if t.strip()]
    exclude = [normalize(t) for t in (args.exclude or "").split(",") if t.strip()]

    anchor_tags = []
    if args.anchor:
        anchor_title, anchor_entry = find_anchor_entry(args.anchor, cache)
        if not anchor_entry:
            print(f"Reference game not found in the cache: {args.anchor}")
            sys.exit(1)
        anchor_tags = anchor_entry.get("tags", [])[:15]
        print(f"Reference game: {anchor_title}")
        print(f"Tags used: {', '.join(anchor_tags)}")

    print("Reading the Heroic library...")
    games = collect_library(heroic_dir)
    by_title = {}
    for g in games:
        by_title.setdefault(normalize(g["title"]), []).append(g)

    candidates = []
    for title, entry in cache.items():
        tags = entry.get("tags", [])
        if not tags:
            continue
        if args.anchor and normalize(title) == normalize(anchor_title):
            continue  # never include the reference game itself

        if require:
            if args.require_all:
                if not all(t in tags for t in require):
                    continue
            else:
                if not any(t in tags for t in require):
                    continue
        if exclude and any(t in tags for t in exclude):
            continue

        score = len(set(tags[:15]) & set(anchor_tags)) if anchor_tags else len(
            [t for t in require if t in tags]
        )
        if anchor_tags and score < args.min_shared:
            continue

        matches = by_title.get(normalize(title), [])
        for game in matches:
            candidates.append(
                {
                    "title": game["title"],
                    "app_name": game["app_name"],
                    "runner": game["runner"],
                    "heroic_id": game["heroic_id"],
                    "score": score,
                    "top_tags": ", ".join(tags[:6]),
                }
            )

    candidates.sort(key=lambda c: c["score"], reverse=True)
    if args.top:
        candidates = candidates[: args.top]

    if not candidates:
        print("No game matches the given criteria.")
        return

    print()
    print(f"{len(candidates)} game(s) found:")
    for c in candidates:
        print(f"  [{c['score']}] {c['title']}  ({c['top_tags']})")

    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["title", "app_name", "runner", "heroic_id", "category", "matched_on", "top_tags"],
        )
        writer.writeheader()
        for c in candidates:
            writer.writerow(
                {
                    "title": c["title"],
                    "app_name": c["app_name"],
                    "runner": c["runner"],
                    "heroic_id": c["heroic_id"],
                    "category": args.category,
                    "matched_on": f"score={c['score']}",
                    "top_tags": c["top_tags"],
                }
            )

    print()
    print(f"File written: {args.output}")
    print("Delete the rows you do not want (or blank their 'category' cell), then:")
    print(f"    python heroic_categorize.py apply {args.output}")


# ----------------------------------------------------------------------------
# Command: apply
# ----------------------------------------------------------------------------


def heroic_still_running():
    """Detect a Heroic process that is still alive (it often just minimises to
    the system tray, which keeps the old config in memory and overwrites our
    changes on the next internal save unless we kill it first)."""
    system = platform.system()
    try:
        if system == "Windows":
            out = os.popen('tasklist /FI "IMAGENAME eq Heroic.exe" /NH').read()
            return "heroic.exe" in out.lower()
        else:
            out = os.popen("ps -A -o comm").read().lower()
            return "heroic" in out
    except Exception:
        return False  # if detection fails, do not block the user


def cmd_apply(args):
    heroic_dir = args.heroic_dir
    config_path = os.path.join(heroic_dir, "store", "config.json")

    if not os.path.isfile(config_path):
        print(f"File not found: {config_path}")
        sys.exit(1)

    if not args.force and heroic_still_running():
        print("!! A Heroic process is still running (often in the system tray")
        print("   even after closing the window). While it runs, it keeps the")
        print("   old config in memory and will overwrite your changes on its")
        print("   next internal save.")
        print()
        print("   Close it completely, e.g. on Windows:")
        print("       taskkill /F /IM Heroic.exe /T")
        print("   or on Linux/macOS:")
        print("       pkill -f -i heroic")
        print("   then run this command again.")
        print("   (If detection is wrong on your system, re-run with --force.)")
        sys.exit(1)

    print("!! Make sure Heroic is CLOSED before continuing !!")
    if not args.yes:
        answer = input("Is Heroic closed? (y/N) ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    with open(args.proposal, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        proposal_rows = list(reader)

    config = load_json(config_path)
    config.setdefault("games", {})
    config["games"].setdefault("customCategories", {})
    custom_categories = config["games"]["customCategories"]

    added = 0
    skipped = 0
    for row in proposal_rows:
        raw_category = (row.get("category") or "").strip()
        # a single row may list several categories separated by ";"
        categories = [c.strip() for c in raw_category.split(";") if c.strip()]

        heroic_id = (row.get("heroic_id") or "").strip()
        if not heroic_id:
            app_name = (row.get("app_name") or "").strip()
            runner = (row.get("runner") or "").strip()
            if app_name and runner and runner != "?":
                heroic_id = f"{app_name}_{runner}"

        if not categories or not heroic_id:
            skipped += 1
            continue

        for category in categories:
            bucket = custom_categories.setdefault(category, [])
            if heroic_id not in bucket:
                bucket.append(heroic_id)
                added += 1

    # back up before writing
    backup_path = config_path + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(config_path, backup_path)
    print(f"Existing config backed up to: {backup_path}")

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"{added} games added to categories ({skipped} rows skipped, no category).")
    print("You can restart Heroic: the categories are applied.")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(description="Automatic Heroic categorisation (via Steam/SteamSpy, no IGDB).")
    parser.add_argument("--heroic-dir", default=default_heroic_dir(), help="Heroic configuration folder")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Analyse the library and propose categories")
    p_scan.add_argument("--mapping", default="mapping.json", help="Tag -> category mapping file")
    p_scan.add_argument("--output", default="proposal.csv", help="Proposal file to generate")
    p_scan.add_argument("--cache", default="steam_cache.json", help="Cache file for Steam/SteamSpy responses")
    p_scan.add_argument("--lang", default="english", help="Language requested from Steam (english, french, ...)")
    p_scan.add_argument("--cc", default="us", help="Country code for Steam (us, fr, ...)")
    p_scan.add_argument("--delay", type=float, default=1.0, help="Delay (s) between two SteamSpy calls")
    p_scan.add_argument("--only-uncategorized", action="store_true", help="Skip games that already have a category")
    p_scan.add_argument("--max-categories", type=int, default=5, help="Max categories proposed per game")
    p_scan.add_argument("--limit", type=int, default=0, help="Cap the number of games processed (0 = all)")
    p_scan.set_defaults(func=cmd_scan)

    p_combos = sub.add_parser("combos", help="Generate AND categories (intersections) from a proposal.csv")
    p_combos.add_argument("proposal", help="CSV file produced by scan (or already passed through apply)")
    p_combos.add_argument("--output", default="proposal_combos.csv", help="Output file to generate")
    p_combos.add_argument("--min-count", type=int, default=3, help="Minimum number of games to keep a combination")
    p_combos.add_argument("--prefix", default="", help="Prefix for combined categories (e.g. '0-' to sort them first)")
    p_combos.set_defaults(func=cmd_combos)

    p_similar = sub.add_parser("similar", help="Find games 'like X' via tag similarity and/or filters")
    p_similar.add_argument("--cache", default="steam_cache.json", help="Steam/SteamSpy cache file (produced by scan)")
    p_similar.add_argument("--anchor", default=None, help="Title of a reference game in your library")
    p_similar.add_argument("--require", default=None, help="Required tags, comma-separated (e.g. 'fps,story rich')")
    p_similar.add_argument("--require-all", action="store_true", help="Require ALL --require tags (default: at least one)")
    p_similar.add_argument("--exclude", default=None, help="Tags to exclude, comma-separated (e.g. 'horror')")
    p_similar.add_argument("--min-shared", type=int, default=2, help="Minimum number of tags shared with --anchor")
    p_similar.add_argument("--top", type=int, default=25, help="Maximum number of games to keep")
    p_similar.add_argument("--category", default="Suggestions", help="Category name to write in the output CSV")
    p_similar.add_argument("--output", default="similar.csv", help="Output file to generate")
    p_similar.set_defaults(func=cmd_similar)

    p_apply = sub.add_parser("apply", help="Apply a proposal file to the Heroic config")
    p_apply.add_argument("proposal", help="CSV file produced (and optionally edited) by the scan command")
    p_apply.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")
    p_apply.add_argument("--force", action="store_true", help="Ignore the running-Heroic detection")
    p_apply.set_defaults(func=cmd_apply)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
