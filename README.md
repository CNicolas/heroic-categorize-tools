# heroic-categorize-tools

Categorise a [Heroic Games Launcher](https://heroicgameslauncher.com) library
automatically. Genres and tags come from the public Steam and SteamSpy APIs —
no account, no API key, no IGDB. One Python file, standard library only.

It also imports the games Heroic cannot see (your Steam library, and games you
own on no PC store at all) so that everything lives in one launcher.

## Install

```bash
git clone https://github.com/CNicolas/heroic-categorize-tools
cd heroic-categorize-tools
```

Python 3.8+, nothing else. Close Heroic before running anything — the tool
checks, because Heroic overwrites its config on exit and would silently undo
your work.

## Three commands

```bash
python heroic-categorize-tools.py full                # first time
python heroic-categorize-tools.py full --with-cache   # redo it, no re-download
python heroic-categorize-tools.py update              # new games only
```

`full` does everything in order: backs up your config, imports your Steam
games and your favourites, asks Steam about every title, applies
`mapping.json`, adds the personal categories from `profiles.json`, and writes
the result into Heroic after asking you. Expect ~20 minutes for 1000 games the
first time (one API call per game, one second apart); seconds on the reruns,
because every answer is cached in `steam_cache.json`.

`update` is the day-to-day version: it only looks at games that are new or
still uncategorised, and retries the titles Steam failed to match last time.

Add `--no-apply` to any of them to stop at the CSV and review it first.

## The three files you edit

| File | Question it answers | Example |
|---|---|---|
| `mapping.json` | What IS this game? | `"local co-op": "Couch Co-op"` |
| `profiles.json` | Who is it FOR? | couch co-op **and** not punishing **and** not sad |
| `excluded-titles.txt` | What should never be suggested again? | games you already finished |

Only `mapping.json` is needed. The other two are optional: drop them and the
personal categories are simply skipped.

**`mapping.json`** maps a Steam tag to a Heroic category. A tag can produce
several categories (`"survival horror": ["Horror", "Survival"]`). The
`_priorities` block decides who wins when a game matches more categories than
`--max-categories` allows: SteamSpy returns tags by descending vote count and
the most-voted tags are the most generic ones, so without a priority table the
informative tags (`local co-op`, `jrpg`, `immersive sim`) get pushed out by
`action` and `adventure`. Higher number = more specific = kept first.

**`profiles.json`** is where taste lives, and it is deliberately separate.
`mapping.json` can only express OR — one tag is enough to trigger a category —
while a personal fit is a conjunction with negations: *local co-op AND NOT
punishing AND NOT depressing*. Rule syntax:

```jsonc
{
  "category": "Couch Duo",
  "require_categories": ["Couch Co-op"],     // at least one mapping.json category
  "require_groups": [
    ["party game", "beat 'em up"],           // list -> at least one of these tags
    { "min": 3, "tags": ["fps", "magic"] }   // dict -> at least N of these tags
  ],
  "require_all": [],                          // every tag mandatory
  "exclude_any": ["souls-like", "difficult"], // one hit disqualifies
  "exclude_categories": ["Challenging"]
}
```

Two levers when tuning: `exclude_any` kills a family of false positives
instantly, and raising a group's `min` from 1 to 3 turns a loose net into a
shortlist.

**`excluded-titles.txt`** is one title per line. No Steam tag will ever encode
"we finished it in 2019", which is exactly why this file exists. Edition
qualifiers, punctuation and trademark symbols are ignored when matching, plus
a 0.90 similarity fallback — but sequels are deliberately not collapsed, so
`Persona 5` does not block `Persona 5 Royal`.

## Optional lists

```bash
cp steam-games.EXAMPLE.txt steam-games.txt              # your Steam library
cp favorites-not-on-pc.EXAMPLE.txt favorites-not-on-pc.txt
```

Steam games get a real launcher script, so they start from Heroic and Steam
does the work behind. Favourites are reference-only entries that open the
Steam page in Heroic's browser — handy for watching a price.

Both are picked up automatically by `full` and `update` if present, and
skipped without complaint if not.

## Everything else

```
scan        propose categories for the Epic/GOG/Amazon library -> proposal.csv
profiles    add the personal categories                        -> proposal_profiles.csv
steam       import your Steam library
favorites   import games owned on no PC store
combos      create AND categories (Action+RPG) from co-occurring pairs
similar     find "games like X" by shared tags
apply       write a proposal CSV into Heroic (--replace to overwrite, not add)
reset       remove categories from Heroic (--keep Steam,Favorites)
cleanup     remove the entries created by steam/favorites
retry       forget the "no Steam match" cache entries so they are looked up again
```

`--help` on any of them.

## Good to know

**`apply` adds, it never removes.** So changing `mapping.json` and re-running
leaves the old categories in place alongside the new ones. `full` handles this
by clearing them first (keeping `Steam` and `Favorites`); elsewhere use
`apply --replace` or `reset`.

**Games with no category are usually missing data, not a missing rule.** They
have no Steam page, or SteamSpy has no tags for them. `retry` (and
`retry --tagless`) is the fix, not a bigger `mapping.json`.

**Nothing is destructive.** `config.json`, `sideload_apps/library.json` and
`steam_cache.json` are copied to a timestamped `.bak-` file before any write.

**Privacy.** `steam_cache.json` holds no credentials, no account ID and no
paths — just `title -> {appid, tags, genre}`. It does inventory your library,
like a public Steam profile would. Same for your two lists and
`excluded-titles.txt`. `.gitignore` excludes them by default.
