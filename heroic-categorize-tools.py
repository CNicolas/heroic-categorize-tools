#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
heroic-categorize-tools.py
==========================

One tool to categorise a Heroic Games Launcher (2.x) library. No IGDB, no
account, no API key: genres and tags come from the public Steam Store and
SteamSpy APIs, and are mapped to Heroic categories through three plain files
you can edit by hand.

    mapping.json          what a game IS      (tag -> category)
    profiles.json         who it is FOR       (taste rules, AND / NOT)
    excluded-titles.txt   what to never suggest again (already played)

MOST PEOPLE ONLY NEED THREE COMMANDS
------------------------------------
    python heroic-categorize-tools.py full              # first time, from scratch
    python heroic-categorize-tools.py full --with-cache # redo it, without re-downloading
    python heroic-categorize-tools.py update            # new games only, day to day

Everything else below is the same work split into smaller steps, for when you
want to review a CSV before it touches Heroic.

    scan        propose categories for the games Heroic knows (Epic/GOG/Amazon)
    steam       import your Steam library into Heroic so it can be categorised
    favorites   import games you own on no PC store, as reference entries
    profiles    add the personal-fit categories from profiles.json
    combos      create AND categories (Action+RPG) from co-occurring pairs
    similar     find "games like X" by tag similarity
    apply       write a proposal CSV into the Heroic config
    reset       remove categories from the Heroic config
    cleanup     remove the entries imported by steam/favorites
    retry       forget the "no Steam match" cache entries so they get retried

Runs on Linux, Windows and macOS. Standard library only.
"""

import argparse
import csv
import itertools
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
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


# Edition/version qualifiers that commonly differ between a store listing and
# the name typed by a user (or exported from a client in another language),
# without changing which actual game is meant. Covers both English and French
# since library exports frequently come from a French-language Steam client.
_EDITION_NOISE_RE = re.compile(
    r"\b("
    r"goty|game of the year( edition)?|"
    r"definitive|complete|enhanced|ultimate|deluxe|special|extended|"
    r"remaster(ed)?|directors? cut|anniversary|classic|redux|reforged|"
    r"version originale|version amelioree|version complete|version integrale|"
    r"edition definitive|edition ultime|edition complete|edition integrale|"
    r"edition|jeu de l annee"
    r")\b",
    re.IGNORECASE,
)


def strip_edition_noise(text):
    """Drop edition/version qualifiers (EN+FR) so 'Grand Theft Auto V Version
    amelioree' and 'Grand Theft Auto V' compare as the same game."""
    t = normalize(text)
    t = _EDITION_NOISE_RE.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return t


def similarity(a, b):
    # Simple similarity; difflib is part of the standard library so we may as
    # well use it rather than rolling our own. Also compare noise-stripped
    # versions and keep the best score: a localized edition suffix
    # ("Version amelioree", "Definitive Edition"...) should not tank an
    # otherwise perfect match.
    import difflib

    raw = difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()
    cleaned_a, cleaned_b = strip_edition_noise(a), strip_edition_noise(b)
    if not cleaned_a or not cleaned_b:
        return raw
    cleaned = difflib.SequenceMatcher(None, cleaned_a, cleaned_b).ratio()
    return max(raw, cleaned)


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


def _steam_search_once(term, lang, cc):
    data = http_get_json(STEAM_SEARCH_URL, {"term": term, "l": lang, "cc": cc})
    if not data or not data.get("items"):
        return None
    best = None
    best_score = 0.0
    for item in data["items"][:5]:
        score = similarity(term, item.get("name", ""))
        if score > best_score:
            best_score = score
            best = item
    if best:
        return best["id"], best_score
    return None


def steam_find_appid(title, lang, cc):
    """Look up a title on the Steam store. Tries the title as given first;
    if that scores too low (or the API returns nothing, which can happen when
    the term itself carries too much noise for full-text search), retries
    once with edition/version qualifiers stripped out."""
    result = _steam_search_once(title, lang, cc)

    cleaned = strip_edition_noise(title)
    if cleaned and cleaned != normalize(title) and (not result or result[1] < 0.6):
        alt = _steam_search_once(cleaned, lang, cc)
        if alt and (not result or alt[1] > result[1]):
            result = alt

    if result and result[1] >= 0.6:
        return result
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


DEFAULT_PRIORITY = 1


def load_mapping(mapping_path):
    """Load mapping.json and return (mapping, priorities).

    Each entry may be either a single category (string) or a list of
    categories (e.g. "survival horror": ["Horror", "Survival"]); internally
    everything is normalised to a list. Keys starting with an underscore are
    metadata and are never treated as tags -- "_priorities" holds an optional
    {category: int} table used to decide which categories survive the
    --max-categories cap (see pick_categories)."""
    with open(mapping_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    priorities = {}
    for k, v in (raw.get("_priorities") or {}).items():
        try:
            priorities[k] = int(v)
        except (TypeError, ValueError):
            continue
    normalized = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if isinstance(v, list):
            normalized[normalize(k)] = list(v)
        else:
            normalized[normalize(k)] = [v]
    return normalized, priorities


def pick_categories(tag_names, genre_str, mapping, max_categories=3, priorities=None):
    """Return an ordered, duplicate-free list of matching categories.

    Every community tag is examined -- not just the ones that fit under the
    cap -- and the resulting categories are then ranked by specificity before
    being truncated to max_categories. This matters because SteamSpy returns
    tags by descending vote count, and the most-voted tags are almost always
    the most generic ones ("action", "adventure", "rpg"): walking the list in
    vote order and stopping at the cap would systematically discard the
    informative tags ("local co-op", "jrpg", "immersive sim") that sit further
    down. Specificity comes from the "_priorities" table in mapping.json;
    ties keep the original vote order, so the cap now means "the N most
    informative categories" rather than "the N most-voted ones".

    The official Steam genres are a pure safety net: they are only consulted
    when no tag at all produced a category, so that a game such as Control
    still lands somewhere instead of coming out empty."""
    priorities = priorities or {}
    found = []
    matched_on = {}

    def collect(key):
        if key not in mapping:
            return
        for cat in mapping[key]:
            if cat not in found:
                found.append(cat)
                matched_on[cat] = key

    for tag in tag_names:
        collect(tag)

    if not found:
        for part in genre_str.split(","):
            collect(part.strip())

    ranked = sorted(found, key=lambda c: -priorities.get(c, DEFAULT_PRIORITY))
    ranked = ranked[:max_categories]
    return ranked, [matched_on[c] for c in ranked]


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

    mapping, priorities = load_mapping(args.mapping)
    print(f"{len(mapping)} tag/genre -> category rules loaded from {args.mapping}")
    if priorities:
        print(f"{len(priorities)} category priorities loaded (specificity ranking enabled)")

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

        categories, matched_on = pick_categories(
            entry["tags"], entry["genre"], mapping, args.max_categories, priorities
        )
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
    print(f"    python heroic-categorize-tools.py apply {args.output}")


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
    print(f"Review it, then: python heroic-categorize-tools.py apply {args.output}")


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
    print(f"    python heroic-categorize-tools.py apply {args.output}")


# ----------------------------------------------------------------------------
# Command: profiles (personal-fit categories, from profiles.json)
# ----------------------------------------------------------------------------
#
# mapping.json answers "what IS this game?" -- a factual question a stranger
# could verify. It can only express OR: one tag is enough to trigger a
# category, and it has no way to say "except this title".
#
# A personal fit is a different kind of statement: it is a conjunction with
# negations plus a list of exceptions ("local co-op AND NOT punishing AND NOT
# depressing AND not one we already finished"). That logic lives here, in
# profiles.json, so that tweaking a taste never means re-auditing the whole
# taxonomy -- and so that a wrong taste rule stays a wrong taste rule instead
# of contaminating the genre categories.


_TITLE_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def blocklist_key(title):
    """Comparison key for the blocklist: no diacritics, no edition noise, no
    punctuation and no store decorations, so that 'LEGO(R) The Hobbit(TM)'
    and 'Lego the Hobbit' collapse to the same string."""
    return _TITLE_PUNCT_RE.sub(" ", normalize(strip_edition_noise(title))).strip()


def load_title_blocklist(paths):
    """Read one or more plain-text files of titles to exclude (one per line,
    '#' starts a comment). Returns a set of normalised, edition-stripped
    titles."""
    blocked = set()
    for path in paths or []:
        if not os.path.isfile(path):
            print(f"  (warning) title blocklist not found, ignored: {path}")
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    blocked.add(blocklist_key(line))
    return blocked


def title_is_blocked(title, blocked, fuzzy=0.90):
    """True if the title is in the blocklist, exactly or near-exactly. The
    fuzzy pass catches store-name drift ('Trine Enchanted Edition' vs
    'Trine') that the exact key would miss."""
    key = blocklist_key(title)
    if key in blocked:
        return True
    for b in blocked:
        if similarity(key, b) >= fuzzy:
            return True
    return False


def load_profiles(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    profiles = raw.get("profiles") or []
    shared_blocklist = raw.get("exclude_title_files") or []
    return profiles, shared_blocklist


def profile_matches(profile, tags, categories):
    """Evaluate one profile rule against a game.

    Supported keys (all optional):
      require_groups        list of tag lists; EACH group must contribute at
                            least one tag  -> this is the AND of ORs
      require_any           shorthand for a single require_groups entry
      require_all           every tag must be present
      require_categories    at least one of these layer-1 categories
      exclude_any           any one of these tags disqualifies the game
      exclude_categories    any one of these layer-1 categories disqualifies
    Returns (matched, reason) -- reason lists the tags that carried the match,
    so the CSV stays auditable."""
    tags = set(tags)
    categories = set(categories)

    for tag in profile.get("require_all") or []:
        if normalize(tag) not in tags:
            return False, ""

    for tag in profile.get("exclude_any") or []:
        if normalize(tag) in tags:
            return False, ""

    for cat in profile.get("exclude_categories") or []:
        if cat in categories:
            return False, ""

    req_cats = profile.get("require_categories") or []
    if req_cats and not (categories & set(req_cats)):
        return False, ""

    groups = list(profile.get("require_groups") or [])
    if profile.get("require_any"):
        groups.append(list(profile["require_any"]))

    hits = []
    for group in groups:
        if isinstance(group, dict):
            group_tags = group.get("tags") or []
            minimum = int(group.get("min", 1))
        else:
            group_tags = group
            minimum = 1
        group_hits = [t for t in group_tags if normalize(t) in tags]
        if len(group_hits) < minimum:
            return False, ""
        hits.extend(group_hits)

    return True, ", ".join(hits[:6])


def cmd_profiles(args):
    cache = load_json(args.cache) or {}
    if not cache:
        print(f"Cache missing or empty: {args.cache}")
        print("Run 'scan' first to populate the Steam/SteamSpy cache.")
        sys.exit(1)

    profiles, shared_blocklist = load_profiles(args.profiles)
    print(f"{len(profiles)} profile(s) loaded from {args.profiles}")

    with open(args.proposal, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"{len(rows)} games read from {args.proposal}")

    # tags are cached per title; build a lookup tolerant to edition noise
    tags_by_key = {}
    for title, entry in cache.items():
        tags_by_key[blocklist_key(title)] = entry.get("tags") or []

    blocklists = {}
    for prof in profiles:
        files = list(shared_blocklist) + list(prof.get("exclude_title_files") or [])
        blocklists[prof["category"]] = load_title_blocklist(files)

    counts = {p["category"]: 0 for p in profiles}
    skipped_known = {p["category"]: 0 for p in profiles}

    for row in rows:
        title = row.get("title") or ""
        cats = [c.strip() for c in (row.get("category") or "").split(";") if c.strip()]
        tags = tags_by_key.get(blocklist_key(title), [])
        added = []
        reasons = []
        for prof in profiles:
            name = prof["category"]
            if name in cats:
                continue
            matched, reason = profile_matches(prof, tags, cats)
            if not matched:
                continue
            if title_is_blocked(title, blocklists[name]):
                skipped_known[name] += 1
                continue
            added.append(name)
            reasons.append(f"{name} <- {reason}")
            counts[name] += 1
        if added:
            row["category"] = "; ".join(cats + added)
            if "matched_on" in row:
                existing = row.get("matched_on") or ""
                row["matched_on"] = "; ".join(x for x in [existing] + reasons if x)

    fieldnames = list(rows[0].keys()) if rows else ["title", "category"]
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print()
    for prof in profiles:
        name = prof["category"]
        print(f"  {counts[name]:4d} games tagged '{name}'"
              f"   ({skipped_known[name]} skipped as already played/rejected)")
    print()
    print(f"File written: {args.output}")
    print(f"Review it, then: python heroic-categorize-tools.py apply {args.output}")


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

    if getattr(args, "replace", False):
        keep = {k.strip() for k in (getattr(args, "keep", "") or "").split(",") if k.strip()}
        touched = set()
        for row in proposal_rows:
            hid = (row.get("heroic_id") or "").strip()
            if hid:
                touched.add(hid)
        removed = 0
        for name, bucket in list(custom_categories.items()):
            if name in keep:
                continue
            kept_ids = [i for i in bucket if i not in touched]
            removed += len(bucket) - len(kept_ids)
            if kept_ids:
                custom_categories[name] = kept_ids
            else:
                del custom_categories[name]
        print(f"--replace: {removed} previous membership(s) dropped for the games in this file.")

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


SIDELOAD_REL = os.path.join("sideload_apps", "library.json")
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"

# Fixed namespace: guarantees the same title always yields the same app_name,
# so a second run updates instead of duplicating.
NS_HEROIC = uuid.UUID("6f0a1c52-0d8e-5a3f-9b21-4c7e5d8a1b30")

# Lines skipped by default in a library list: demos, betas, tools, DLC,
# editors... everything that would just clutter the Heroic library.
DEFAULT_SKIP = [
    r"\bdemo\b",
    r"\bbeta\b",
    r"public test",
    r"\bsdk\b",
    r"\beditor\b",
    r"resource archiver",
    r"pre-game editor",
    r"\btest branch\b",
    r"soundtrack",
    r"\bost\b",
    r"art ?book",
    r"wallpaper",
    r"parts pack",
    r"free weekend",
    r"redistributables",
    r"\bdlc\b",
]


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def heroic_platform():
    """The value Heroic expects in install.platform for a native binary."""
    system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Darwin":
        return "Mac"
    return "linux"


def launcher_extension():
    return ".cmd" if platform.system() == "Windows" else ".sh"


def launcher_body(title, appid):
    """A tiny script that hands the steam:// URL to the OS. The exact form
    differs per platform, which is why this is not a one-liner."""
    system = platform.system()
    url = f"steam://rungameid/{appid}"
    if system == "Windows":
        return (
            "@echo off\r\n"
            f"rem Launches \"{title}\" via Steam (appid {appid}).\r\n"
            "rem Generated by heroic-categorize-tools.py -- edit freely.\r\n"
            f"start \"\" \"{url}\"\r\n"
        )
    if system == "Darwin":
        return (
            "#!/usr/bin/env bash\n"
            f"# Launches \"{title}\" via Steam (appid {appid}).\n"
            "# Generated by heroic-categorize-tools.py -- edit freely.\n"
            f"exec open \"{url}\"\n"
        )
    return (
        "#!/usr/bin/env bash\n"
        f"# Launches \"{title}\" via Steam (appid {appid}).\n"
        "# Generated by heroic-categorize-tools.py -- edit freely.\n"
        f"exec steam {url}\n"
    )


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def title_key(title):
    """Forgiving comparison key: lowercase, unaccented, unpunctuated, without
    the marketing suffixes that differ from one store to the next."""
    t = normalize(title)
    t = re.sub(r"[\u2122\u00ae]", " ", t)
    t = re.sub(
        r"\b(the|a|of|and|edition|definitive|remastered|remaster|complete|goty|"
        r"game of the year|enhanced|ultimate|special|deluxe|extended|classic|"
        r"anniversary|redux|reforged)\b",
        " ",
        t,
    )
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def app_name_for(title):
    """Stable 32-character hex identifier, matching Heroic's app_name format."""
    return uuid.uuid5(NS_HEROIC, title_key(title)).hex


def read_list(path):
    if not os.path.isfile(path):
        print(f"File not found: {path}")
        sys.exit(1)
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                # Some library exporters insert a stray space before an
                # apostrophe (e.g. "Lovers ' Smiles"), which throws off both
                # the Steam search and the dedup key. Tidy that up.
                line = re.sub(r"\s+(['\u2019])", r"\1", line)
                out.append(line)
    return out


def should_skip(title, patterns):
    low = title.lower()
    return any(re.search(p, low) for p in patterns)


def load_cache(path):
    if os.path.isfile(path):
        return load_json(path) or {}
    return {}


def save_cache(cache, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def lookup_steam(title, cache, cache_path, lang, cc, delay):
    """Return the cache entry {appid, tags, genre} for a title, hitting
    Steam/SteamSpy only when necessary."""
    if title in cache:
        return cache[title], True
    found = steam_find_appid(title, lang, cc)
    if not found:
        entry = {"appid": None, "tags": [], "genre": ""}
    else:
        appid, _score = found
        tags, genre = steamspy_get_tags_and_genre(appid)
        entry = {"appid": appid, "tags": tags, "genre": genre}
        time.sleep(delay)
    cache[title] = entry
    save_cache(cache, cache_path)
    return entry, False


# ---------------------------------------------------------------------------
# Writing to sideload_apps/library.json
# ---------------------------------------------------------------------------


def load_sideload(heroic_dir):
    path = os.path.join(heroic_dir, SIDELOAD_REL)
    data = load_json(path)
    if data is None:
        data = {"games": []}
    data.setdefault("games", [])
    return path, data


def art_for(appid):
    if not appid:
        return "", ""
    return (
        f"{STEAM_CDN}/{appid}/header.jpg",
        f"{STEAM_CDN}/{appid}/library_600x900.jpg",
    )


def steam_search_url(title):
    return "https://store.steampowered.com/search/?term=" + urllib.parse.quote(title)


def make_launcher(launch_dir, title, appid):
    """Write the small launcher script and return its absolute path."""
    os.makedirs(launch_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or str(appid)
    path = os.path.join(launch_dir, safe + launcher_extension())
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(launcher_body(title, appid))
    if platform.system() != "Windows":
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def build_entry(title, appid, kind, launcher_path=None, launch_dir=None):
    """Build a sideload entry in the shape Heroic expects.

    kind == "steam" with an appid  : genuinely launchable (executable = launcher script)
    kind == "steam" without an appid : no Steam page was found automatically,
        so there is nothing to launch yet -- created as a browser placeholder
        pointing at a Steam *search* for the title, so you can look it up,
        fix the title, or add the real launcher by hand later.
    kind == "favorites"             : browser entry pointing at the Steam store page
        (or a search page, if no exact page was found -- e.g. games that only
        ever existed on a console or another launcher, with no Steam page at all)
    """
    app_name = app_name_for(title)
    cover, square = art_for(appid)
    store_url = f"https://store.steampowered.com/app/{appid}" if appid else ""

    entry = {
        "runner": "sideload",
        "app_name": app_name,
        "title": title,
        "art_cover": cover,
        "art_square": square,
        "is_installed": True,
        "canRunOffline": kind == "steam" and bool(appid),
    }

    if kind == "steam" and appid:
        entry["install"] = {
            "executable": launcher_path,
            "platform": heroic_platform(),
            "is_installed": True,
        }
        entry["folder_name"] = launch_dir
        entry["description"] = "Steam game imported into Heroic for categorisation."
    elif kind == "steam":
        entry["install"] = {"platform": "Browser", "is_installed": True}
        entry["browserUrl"] = steam_search_url(title)
        entry["description"] = (
            "No Steam page found automatically -- placeholder pointing at a "
            "Steam search for this title. Fix the title and re-run, or "
            "replace this entry by hand once you have found the right game."
        )
    else:
        entry["install"] = {"platform": "Browser", "is_installed": True}
        entry["browserUrl"] = store_url or steam_search_url(title)
        entry["description"] = "Reference entry (not owned on PC) -- opens the Steam store page."

    return entry


def merge_sideload(data, entries):
    """Merge by app_name: update what exists, append the rest."""
    by_app = {g.get("app_name"): i for i, g in enumerate(data["games"]) if g.get("app_name")}
    added = updated = 0
    for e in entries:
        idx = by_app.get(e["app_name"])
        if idx is None:
            data["games"].append(e)
            by_app[e["app_name"]] = len(data["games"]) - 1
            added += 1
        else:
            data["games"][idx].update(e)
            updated += 1
    return added, updated


def write_sideload(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.isfile(path):
        backup = path + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, backup)
        print(f"Backed up to: {backup}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_proposal(rows, output):
    with open(output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["title", "app_name", "runner", "heroic_id", "category", "matched_on", "top_tags"],
        )
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Shared core of both subcommands
# ---------------------------------------------------------------------------


def run_import(args, kind):
    heroic_dir = args.heroic_dir
    if not os.path.isdir(heroic_dir):
        print(f"Heroic folder not found: {heroic_dir}")
        print("Point at it with --heroic-dir.")
        sys.exit(1)

    if not args.dry_run and not args.force and heroic_still_running():
        print("!! Heroic is still running (often minimised to the system tray).")
        print("   It would overwrite these changes. Close it first:")
        if platform.system() == "Windows":
            print("       taskkill /F /IM Heroic.exe /T")
        else:
            print("       pkill -f -i heroic")
        print("   then run again. (--force to override, --dry-run to test.)")
        sys.exit(1)

    mapping, priorities = load_mapping(args.mapping)
    print(f"{len(mapping)} tag -> category rules loaded.")

    # 1. What Heroic already knows about
    print("Reading the Heroic library...")
    library = collect_library(heroic_dir)
    known = {title_key(g["title"]) for g in library}
    print(f"{len(library)} games already present in Heroic.")

    # 2. What else to skip (other stores, for the favorites subcommand)
    excluded = set()
    for path in args.exclude_list or []:
        for t in read_list(path):
            excluded.add(title_key(t))
    if excluded:
        print(f"{len(excluded)} titles loaded from the exclusion lists.")

    skip_patterns = list(DEFAULT_SKIP)
    if args.no_default_skip:
        skip_patterns = []
    if args.skip:
        skip_patterns += [args.skip]

    # 3. Filter the input list
    wanted = read_list(args.list)
    todo, ignored = [], []
    for title in wanted:
        key = title_key(title)
        if not key:
            continue
        if should_skip(title, skip_patterns):
            ignored.append((title, "filtered out (demo/tool/DLC)"))
        elif key in known:
            ignored.append((title, "already in Heroic"))
        elif key in excluded:
            ignored.append((title, "present in another library"))
        else:
            todo.append(title)

    print(f"{len(todo)} games to import, {len(ignored)} skipped.")
    if args.verbose:
        for title, why in ignored:
            print(f"  - {title}  [{why}]")
    if args.limit:
        todo = todo[: args.limit]
        print(f"Capped at {len(todo)} for this run.")
    if not todo:
        print("Nothing to do.")
        return

    # 4. Steam lookup + categorisation
    cache = load_cache(args.cache)
    launch_dir = os.path.abspath(os.path.expanduser(args.launchers_dir))
    entries, rows, unmatched = [], [], []

    for i, title in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {title}", end=" ... ")
        entry, cached = lookup_steam(title, cache, args.cache, args.lang, args.cc, args.delay)
        appid = entry.get("appid")
        print("(cached)" if cached else (f"appid={appid}" if appid else "no Steam match"))

        if not appid:
            unmatched.append(title)
            if args.skip_unmatched:
                continue

        launcher = None
        if kind == "steam" and appid and not args.no_launcher:
            launcher = make_launcher(launch_dir, title, appid)

        game = build_entry(title, appid, kind, launcher, launch_dir)
        entries.append(game)

        categories, matched_on = pick_categories(
            entry.get("tags", []), entry.get("genre", ""), mapping, args.max_categories, priorities
        )
        for extra in args.extra_category or []:
            if extra not in categories:
                categories.insert(0, extra)

        rows.append(
            {
                "title": title,
                "app_name": game["app_name"],
                "runner": "sideload",
                "heroic_id": f"{game['app_name']}_sideload",
                "category": "; ".join(categories),
                "matched_on": "; ".join(matched_on),
                "top_tags": ", ".join(entry.get("tags", [])[:6]),
            }
        )

    # 5. Write everything out
    write_proposal(rows, args.output)
    print()
    print(f"Proposal written: {args.output}  ({len(rows)} rows)")

    sideload_path, data = load_sideload(heroic_dir)
    added, updated = merge_sideload(data, entries)

    if args.dry_run:
        print(f"[dry-run] {added} entries would be added, {updated} updated "
              f"in {sideload_path}. Nothing was written.")
    else:
        write_sideload(sideload_path, data)
        print(f"{added} entries added, {updated} updated in {sideload_path}")
        if kind == "steam" and not args.no_launcher:
            print(f"Launcher scripts generated in: {launch_dir}")

    if unmatched:
        print()
        if args.skip_unmatched:
            print(f"{len(unmatched)} title(s) with no Steam match, skipped entirely "
                  f"(--skip-unmatched): not in the CSV, not added to Heroic.")
        elif kind == "steam":
            print(f"{len(unmatched)} title(s) with no Steam match: added anyway, as a "
                  f"non-launchable placeholder pointing at a Steam search, with an "
                  f"empty category to fill in by hand:")
        else:
            print(f"{len(unmatched)} title(s) with no Steam match (often games with no "
                  f"Steam page at all -- Battle.net, a console, a launcher of their own): "
                  f"added with an empty category to fill in by hand:")
        for t in unmatched:
            print(f"  - {t}")

    print()
    print("Review/fix the 'category' column, then with Heroic closed:")
    print(f"    python heroic-categorize-tools.py apply {args.output}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_steam(args):
    run_import(args, "steam")


def cmd_favorites(args):
    run_import(args, "favorites")


# ----------------------------------------------------------------------------
# Small helpers shared by the high-level commands
# ----------------------------------------------------------------------------


def say(title):
    """Print a visible step header so a long run stays readable."""
    print()
    print("=" * 74)
    print(f"  {title}")
    print("=" * 74)


def ask_yes(question, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "o", "oui")


def require_heroic_closed(force=False):
    """Block until Heroic is really closed -- it silently overwrites the config
    on its next internal save otherwise, which is the single most common way to
    lose a run's work."""
    while heroic_still_running():
        if force:
            print("!! Heroic still running, continuing anyway (--force).")
            return
        print()
        print("!! Heroic is still running (it often just minimises to the tray).")
        print("   Close it completely:")
        if platform.system() == "Windows":
            print("       taskkill /F /IM Heroic.exe /T")
        else:
            print("       pkill -f -i heroic")
        if not ask_yes("   Retry the check?", default=True):
            print("Aborted. Nothing was written.")
            sys.exit(1)
    print("Heroic is closed. Good.")


class Bag:
    """Tiny argparse.Namespace stand-in, so the preset commands can call the
    building blocks without shelling out to themselves."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return None


def backup_file(path, label):
    if not os.path.isfile(path):
        return None
    dest = path + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, dest)
    print(f"  {label} backed up to {dest}")
    return dest


def file_or_none(path):
    return path if path and os.path.isfile(path) else None


# ----------------------------------------------------------------------------
# Command: reset (remove categories before re-categorising)
# ----------------------------------------------------------------------------


def wipe_categories(config_path, keep=(), quiet=False):
    """Empty customCategories, keeping the categories named in `keep`.

    apply() is additive by design -- it never removes a game from a category.
    That is the right behaviour for a normal run, but it means that changing
    mapping.json leaves every previous category in place: you end up with the
    old taxonomy and the new one side by side. Re-categorising from scratch
    therefore starts here."""
    config = load_json(config_path) or {}
    cats = config.get("games", {}).get("customCategories", {})
    kept = {name: ids for name, ids in cats.items() if name in keep}
    removed = len(cats) - len(kept)
    config.setdefault("games", {})["customCategories"] = kept
    backup_file(config_path, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    if not quiet:
        print(f"  {removed} categor(ies) removed, {len(kept)} kept ({', '.join(kept) or 'none'}).")
    return removed


def cmd_reset(args):
    config_path = os.path.join(args.heroic_dir, "store", "config.json")
    if not os.path.isfile(config_path):
        print(f"File not found: {config_path}")
        sys.exit(1)
    keep = [k.strip() for k in (args.keep or "").split(",") if k.strip()]
    require_heroic_closed(args.force)
    config = load_json(config_path) or {}
    cats = config.get("games", {}).get("customCategories", {})
    print(f"{len(cats)} categories currently in Heroic:")
    for name in sorted(cats):
        mark = "  (kept)" if name in keep else ""
        print(f"   - {name} ({len(cats[name])} games){mark}")
    if not args.yes and not ask_yes("Remove them?", default=False):
        print("Aborted.")
        return
    wipe_categories(config_path, keep)
    print("Done. Restart Heroic.")


# ----------------------------------------------------------------------------
# Command: cleanup (remove the sideloaded Steam / favourites entries)
# ----------------------------------------------------------------------------


def cmd_cleanup(args):
    """Remove the entries created by the steam / favorites commands.

    They are the only ones carrying a sideload runner and one of our own
    descriptions, so nothing you added by hand through Heroic's own
    "Add Game" button is touched."""
    path = os.path.join(args.heroic_dir, SIDELOAD_REL)
    data = load_json(path)
    if not data:
        print(f"Nothing to clean: {path} not found.")
        return

    games = data.get("games", [])
    ours = [g for g in games if is_imported_entry(g)]
    if not ours:
        print("No imported entry found. Nothing to do.")
        return

    print(f"{len(ours)} imported entr(ies) found, out of {len(games)} sideloaded games:")
    for g in ours[:15]:
        print(f"   - {g.get('title')}")
    if len(ours) > 15:
        print(f"   ... and {len(ours) - 15} more")

    require_heroic_closed(args.force)
    if not args.yes and not ask_yes("Remove them from Heroic?", default=False):
        print("Aborted.")
        return

    backup_file(path, "sideload library.json")
    data["games"] = [g for g in games if not is_imported_entry(g)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"{len(ours)} entries removed. {len(data['games'])} sideloaded games left.")

    if args.launchers_dir:
        launch_dir = os.path.expanduser(args.launchers_dir)
        if os.path.isdir(launch_dir) and ask_yes(f"Also delete {launch_dir}?", default=False):
            shutil.rmtree(launch_dir)
            print("Launcher scripts removed.")
    print("Done. Restart Heroic.")


def is_imported_entry(game):
    description = game.get("description") or ""
    return "imported into Heroic" in description or "Reference entry" in description


# ----------------------------------------------------------------------------
# Command: retry (drop the "no Steam match" cache entries)
# ----------------------------------------------------------------------------


def cmd_retry(args):
    cache = load_json(args.cache)
    if cache is None:
        print(f"No cache file at {args.cache} -- nothing to do.")
        return
    dead = [t for t, e in cache.items() if not (e or {}).get("appid")]
    if getattr(args, "tagless", False):
        dead += [t for t, e in cache.items()
                 if (e or {}).get("appid") and not (e or {}).get("tags")]
    if not dead:
        print("No unmatched entry in the cache. Nothing to do.")
        return
    print(f"{len(dead)} title(s) cached as 'no Steam match':")
    for t in dead[:10]:
        print(f"   - {t}")
    if len(dead) > 10:
        print(f"   ... and {len(dead) - 10} more")
    if not args.yes and not ask_yes("Forget them so the next run retries them?", default=True):
        print("Aborted.")
        return
    backup_file(args.cache, "steam_cache.json")
    for t in dead:
        del cache[t]
    with open(args.cache, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"{len(dead)} entries dropped. They will be looked up again on the next run.")


# ----------------------------------------------------------------------------
# Presets: full / update
# ----------------------------------------------------------------------------


def common_files(args):
    """Resolve the optional input lists once, and say out loud what will and
    will not be processed -- a missing file should never be a silent no-op."""
    steam_list = file_or_none(args.steam_list)
    fav_list = file_or_none(args.favorites_list)
    print(f"  mapping           : {args.mapping}")
    print(f"  profiles          : {file_or_none(args.profiles) or '(none, personal categories skipped)'}")
    print(f"  Steam list        : {steam_list or '(none, Steam import skipped)'}")
    print(f"  favourites list   : {fav_list or '(none, favourites import skipped)'}")
    print(f"  cache             : {args.cache}")
    return steam_list, fav_list


def run_scan_and_profiles(args, only_uncategorized):
    """scan -> profiles, returning the CSV that should be applied."""
    say("Reading Heroic and asking Steam/SteamSpy about every game")
    scan_args = Bag(
        heroic_dir=args.heroic_dir, mapping=args.mapping, output="proposal.csv",
        cache=args.cache, lang=args.lang, cc=args.cc, delay=args.delay,
        only_uncategorized=only_uncategorized, max_categories=args.max_categories,
        limit=args.limit,
    )
    cmd_scan(scan_args)

    profiles_file = file_or_none(args.profiles)
    if not profiles_file:
        return "proposal.csv"

    say("Adding the personal categories from profiles.json")
    cmd_profiles(Bag(proposal="proposal.csv", profiles=profiles_file,
                     cache=args.cache, output="proposal_profiles.csv"))
    return "proposal_profiles.csv"


def maybe_apply(args, proposal, keep=()):
    """Offer to write the result into Heroic right away. Reviewing the CSV
    first is still possible -- answering 'no' just stops here and prints the
    command to run later."""
    if args.no_apply:
        print()
        print(f"Proposal ready: {proposal}")
        print(f"Review it, then: python {os.path.basename(__file__)} apply {proposal}")
        return False
    if not args.yes and not ask_yes(f"Apply {proposal} to Heroic now?", default=True):
        print(f"Not applied. Run it later with: python {os.path.basename(__file__)} apply {proposal}")
        return False
    require_heroic_closed(args.force)
    cmd_apply(Bag(heroic_dir=args.heroic_dir, proposal=proposal, yes=True,
                  force=True, replace=getattr(args, "replace", False), keep=",".join(keep)))
    return True


def cmd_full(args):
    """Everything, in the right order, from an empty Heroic to a fully
    categorised one."""
    say("Full run")
    steam_list, fav_list = common_files(args)
    config_path = os.path.join(args.heroic_dir, "store", "config.json")

    if not args.with_cache and os.path.isfile(args.cache):
        cached = len(load_json(args.cache) or {})
        print()
        print(f"The cache holds {cached} games. Ignoring it means asking Steam about")
        print(f"every single one again: roughly {max(1, int(cached * args.delay / 60))} minutes.")
        if not args.yes and not ask_yes("Really start from scratch?", default=False):
            print("Keeping the cache (same as --with-cache).")
            args.with_cache = True
    if not args.with_cache:
        backup_file(args.cache, "steam_cache.json")
        if os.path.isfile(args.cache):
            os.remove(args.cache)
            print("  cache cleared")

    require_heroic_closed(args.force)

    say("Backing up the Heroic configuration")
    backup_file(config_path, "config.json")
    backup_file(os.path.join(args.heroic_dir, SIDELOAD_REL), "sideload library.json")

    if steam_list:
        say("Importing your Steam library into Heroic")
        cmd_steam(Bag(
            heroic_dir=args.heroic_dir, list=steam_list, exclude_list=[],
            mapping=args.mapping, cache=args.cache, output="proposal_steam.csv",
            extra_category=["Steam"], max_categories=args.max_categories,
            lang=args.lang, cc=args.cc, delay=args.delay, skip=None,
            no_default_skip=False, launchers_dir=args.launchers_dir,
            skip_unmatched=False, limit=args.limit, dry_run=False, force=True,
            verbose=False, no_launcher=False,
        ))
        cmd_apply(Bag(heroic_dir=args.heroic_dir, proposal="proposal_steam.csv",
                      yes=True, force=True, replace=False, keep=""))

    if fav_list:
        say("Importing the games you own on no PC store")
        excludes = [p for p in [steam_list] if p]
        cmd_favorites(Bag(
            heroic_dir=args.heroic_dir, list=fav_list, exclude_list=excludes,
            mapping=args.mapping, cache=args.cache, output="proposal_favorites.csv",
            extra_category=["Favorites"], max_categories=args.max_categories,
            lang=args.lang, cc=args.cc, delay=args.delay, skip=None,
            no_default_skip=False, launchers_dir=args.launchers_dir,
            skip_unmatched=False, limit=args.limit, dry_run=False, force=True,
            verbose=False, no_launcher=True,
        ))
        cmd_apply(Bag(heroic_dir=args.heroic_dir, proposal="proposal_favorites.csv",
                      yes=True, force=True, replace=False, keep=""))

    keep = [k.strip() for k in (args.keep or "").split(",") if k.strip()]
    if args.fresh_categories:
        say("Clearing the categories currently in Heroic")
        print("  (they came from a previous run; the new ones replace them)")
        wipe_categories(config_path, keep)

    proposal = run_scan_and_profiles(args, only_uncategorized=False)
    say("Writing the result into Heroic")
    args.replace = True
    maybe_apply(args, proposal, keep=keep)
    final_summary(proposal)


def cmd_update(args):
    """Day-to-day run: only what is new or still missing."""
    say("Update")
    steam_list, fav_list = common_files(args)

    require_heroic_closed(args.force)

    if args.retry_unmatched:
        say("Retrying the titles Steam had never matched")
        cmd_retry(Bag(cache=args.cache, yes=True, tagless=False))

    if steam_list:
        say("Importing Steam titles added since last time")
        cmd_steam(Bag(
            heroic_dir=args.heroic_dir, list=steam_list, exclude_list=[],
            mapping=args.mapping, cache=args.cache, output="proposal_steam.csv",
            extra_category=["Steam"], max_categories=args.max_categories,
            lang=args.lang, cc=args.cc, delay=args.delay, skip=None,
            no_default_skip=False, launchers_dir=args.launchers_dir,
            skip_unmatched=False, limit=args.limit, dry_run=False, force=True,
            verbose=False, no_launcher=False,
        ))
        cmd_apply(Bag(heroic_dir=args.heroic_dir, proposal="proposal_steam.csv",
                      yes=True, force=True, replace=False, keep=""))

    if fav_list:
        say("Importing favourites added since last time")
        cmd_favorites(Bag(
            heroic_dir=args.heroic_dir, list=fav_list,
            exclude_list=[p for p in [steam_list] if p],
            mapping=args.mapping, cache=args.cache, output="proposal_favorites.csv",
            extra_category=["Favorites"], max_categories=args.max_categories,
            lang=args.lang, cc=args.cc, delay=args.delay, skip=None,
            no_default_skip=False, launchers_dir=args.launchers_dir,
            skip_unmatched=False, limit=args.limit, dry_run=False, force=True,
            verbose=False, no_launcher=True,
        ))
        cmd_apply(Bag(heroic_dir=args.heroic_dir, proposal="proposal_favorites.csv",
                      yes=True, force=True, replace=False, keep=""))

    proposal = run_scan_and_profiles(args, only_uncategorized=not args.all)
    say("Writing the result into Heroic")
    maybe_apply(args, proposal)
    final_summary(proposal)


def final_summary(proposal):
    """Print what the run produced, biggest category first."""
    if not os.path.isfile(proposal):
        return
    counts = {}
    empty = 0
    with open(proposal, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cats = [c.strip() for c in (row.get("category") or "").split(";") if c.strip()]
            if not cats:
                empty += 1
            for c in cats:
                counts[c] = counts.get(c, 0) + 1
    say("Result")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {name}")
    if empty:
        print()
        print(f"  {empty} games got no category at all. That is almost always missing")
        print(f"  data rather than a missing rule: they have no Steam page or")
        print(f"  SteamSpy knows no tag for them. Nothing to fix in mapping.json.")
    print()
    print("Restart Heroic: the categories are there.")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def add_shared_inputs(p):
    p.add_argument("--mapping", default="mapping.json", help="Tag -> category table")
    p.add_argument("--profiles", default="profiles.json", help="Personal-fit rules (skipped if absent)")
    p.add_argument("--steam-list", default="steam-games.txt", help="Your Steam library (skipped if absent)")
    p.add_argument("--favorites-list", default="favorites-not-on-pc.txt",
                   help="Games owned on no PC store (skipped if absent)")
    p.add_argument("--cache", default="steam_cache.json", help="Steam/SteamSpy cache")
    p.add_argument("--launchers-dir", default="~/heroic-steam-launchers",
                   help="Where the Steam launcher scripts go")
    p.add_argument("--max-categories", type=int, default=5, help="Max categories per game")
    p.add_argument("--lang", default="english", help="Language requested from Steam")
    p.add_argument("--cc", default="us", help="Steam country code")
    p.add_argument("--delay", type=float, default=1.0, help="Delay between two SteamSpy calls")
    p.add_argument("--limit", type=int, default=0, help="Cap the number of games (testing)")
    p.add_argument("--no-apply", action="store_true", help="Stop at the CSV, write nothing to Heroic")
    p.add_argument("-y", "--yes", action="store_true", help="Answer yes to every question")
    p.add_argument("--force", action="store_true", help="Ignore the running-Heroic detection")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Categorise a Heroic Games Launcher library (Steam/SteamSpy data, no account).",
        epilog="Start with: full  (first time)  /  update  (afterwards)",
    )
    parser.add_argument("--heroic-dir", default=default_heroic_dir(),
                        help="Heroic configuration folder")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- presets -----------------------------------------------------------
    p_full = sub.add_parser("full", help="Everything, from scratch (the first-time command)")
    add_shared_inputs(p_full)
    p_full.add_argument("--with-cache", action="store_true",
                        help="Reuse the Steam data already downloaded (much faster)")
    p_full.add_argument("--keep-categories", dest="keep", default="Steam,Favorites",
                        help="Categories to preserve when clearing (comma-separated)")
    p_full.add_argument("--no-fresh-categories", dest="fresh_categories", action="store_false",
                        help="Add to the existing categories instead of replacing them")
    p_full.set_defaults(func=cmd_full, fresh_categories=True)

    p_update = sub.add_parser("update", help="Only the new or still-uncategorised games")
    add_shared_inputs(p_update)
    p_update.add_argument("--all", action="store_true",
                          help="Re-examine every game, not just the uncategorised ones")
    p_update.add_argument("--no-retry-unmatched", dest="retry_unmatched", action="store_false",
                          help="Do not retry the titles Steam never matched")
    p_update.set_defaults(func=cmd_update, retry_unmatched=True)

    # ---- building blocks ---------------------------------------------------
    p_scan = sub.add_parser("scan", help="Propose categories for the Epic/GOG/Amazon library")
    p_scan.add_argument("--mapping", default="mapping.json")
    p_scan.add_argument("--output", default="proposal.csv")
    p_scan.add_argument("--cache", default="steam_cache.json")
    p_scan.add_argument("--lang", default="english")
    p_scan.add_argument("--cc", default="us")
    p_scan.add_argument("--delay", type=float, default=1.0)
    p_scan.add_argument("--only-uncategorized", action="store_true")
    p_scan.add_argument("--max-categories", type=int, default=5)
    p_scan.add_argument("--limit", type=int, default=0)
    p_scan.set_defaults(func=cmd_scan)

    p_profiles = sub.add_parser("profiles", help="Add the personal categories from profiles.json")
    p_profiles.add_argument("proposal", help="CSV produced by scan")
    p_profiles.add_argument("--profiles", default="profiles.json")
    p_profiles.add_argument("--cache", default="steam_cache.json")
    p_profiles.add_argument("--output", default="proposal_profiles.csv")
    p_profiles.set_defaults(func=cmd_profiles)

    p_steam = sub.add_parser("steam", help="Import Steam games missing from Heroic")
    add_import_options(p_steam, "proposal_steam.csv", ["Steam"])
    p_steam.add_argument("--no-launcher", action="store_true",
                         help="Reference-only entries, not launchable")
    p_steam.set_defaults(func=cmd_steam)

    p_fav = sub.add_parser("favorites", help="Import games you own on no PC store")
    add_import_options(p_fav, "proposal_favorites.csv", ["Favorites"])
    p_fav.set_defaults(func=cmd_favorites, no_launcher=True)

    p_combos = sub.add_parser("combos", help="Create AND categories from a proposal CSV")
    p_combos.add_argument("proposal")
    p_combos.add_argument("--output", default="proposal_combos.csv")
    p_combos.add_argument("--min-count", type=int, default=3)
    p_combos.add_argument("--prefix", default="")
    p_combos.set_defaults(func=cmd_combos)

    p_similar = sub.add_parser("similar", help="Find games like X")
    p_similar.add_argument("--cache", default="steam_cache.json")
    p_similar.add_argument("--anchor", default=None)
    p_similar.add_argument("--require", default=None)
    p_similar.add_argument("--require-all", action="store_true")
    p_similar.add_argument("--exclude", default=None)
    p_similar.add_argument("--min-shared", type=int, default=2)
    p_similar.add_argument("--top", type=int, default=25)
    p_similar.add_argument("--category", default="Suggestions")
    p_similar.add_argument("--output", default="similar.csv")
    p_similar.set_defaults(func=cmd_similar)

    p_apply = sub.add_parser("apply", help="Write a proposal CSV into the Heroic config")
    p_apply.add_argument("proposal")
    p_apply.add_argument("-y", "--yes", action="store_true")
    p_apply.add_argument("--force", action="store_true")
    p_apply.add_argument("--replace", action="store_true",
                         help="Drop each game's current categories instead of adding to them")
    p_apply.add_argument("--keep", default="Steam,Favorites",
                         help="Categories --replace must not remove")
    p_apply.set_defaults(func=cmd_apply)

    # ---- housekeeping ------------------------------------------------------
    p_reset = sub.add_parser("reset", help="Remove categories from the Heroic config")
    p_reset.add_argument("--keep", default="", help="Categories to preserve (comma-separated)")
    p_reset.add_argument("-y", "--yes", action="store_true")
    p_reset.add_argument("--force", action="store_true")
    p_reset.set_defaults(func=cmd_reset)

    p_cleanup = sub.add_parser("cleanup", help="Remove the entries imported by steam/favorites")
    p_cleanup.add_argument("--launchers-dir", default="~/heroic-steam-launchers")
    p_cleanup.add_argument("-y", "--yes", action="store_true")
    p_cleanup.add_argument("--force", action="store_true")
    p_cleanup.set_defaults(func=cmd_cleanup)

    p_retry = sub.add_parser("retry", help="Forget the 'no Steam match' cache entries")
    p_retry.add_argument("--cache", default="steam_cache.json")
    p_retry.add_argument("--tagless", action="store_true",
                         help="Also retry games matched on Steam but with no SteamSpy tags")
    p_retry.add_argument("-y", "--yes", action="store_true")
    p_retry.set_defaults(func=cmd_retry)

    return parser


def add_import_options(p, default_output, default_extra):
    p.add_argument("--list", required=True, help="Text file, one title per line")
    p.add_argument("--exclude-list", action="append", default=[],
                   help="List of titles NOT to import (repeatable)")
    p.add_argument("--mapping", default="mapping.json")
    p.add_argument("--cache", default="steam_cache.json")
    p.add_argument("--output", default=default_output)
    p.add_argument("--extra-category", action="append", default=list(default_extra))
    p.add_argument("--max-categories", type=int, default=5)
    p.add_argument("--lang", default="english")
    p.add_argument("--cc", default="us")
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--skip", default=None, help="Additional regex pattern to skip")
    p.add_argument("--no-default-skip", action="store_true",
                   help="Disable the demo/beta/tool/DLC filter")
    p.add_argument("--launchers-dir", default="~/heroic-steam-launchers")
    p.add_argument("--skip-unmatched", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true")


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
