# heroic-categorize-tools

Sort a [Heroic Games Launcher](https://heroicgameslauncher.com) library into
categories automatically. Genres and tags come from the public Steam and
SteamSpy APIs — no account, no API key, no IGDB. One Python file, standard
library only.

It also imports the games Heroic cannot see (your Steam library, and games you
own on no PC store at all), so one launcher shows everything.

---

# Quick start

⚠️ Close Heroic first.⚠️

```bash
git clone https://github.com/CNicolas/heroic-categorize-tools
cd heroic-categorize-tools
python3 heroic-categorize-tools.py full
```

That is it — the tool asks before writing anything, and
backs up your config beforehand. Expect ~20 minutes for 1000 games the first
time (one Steam call per game), then seconds, because every answer is cached.

Afterwards:

```bash
python3 heroic-categorize-tools.py update            # you bought new games
python3 heroic-categorize-tools.py exclude "Hades"   # you finished one
```

Three optional files make the result personal. All of them are plain text and
none is required:

| File | Answers | Example line |
|---|---|---|
| `mapping.json` | what a game **is** | `"local co-op": "Couch Co-op"` |
| `profiles.json` | who it is **for** | couch co-op **and** not punishing **and** not sad |
| `excluded-titles.txt` | what you already **played** | `Hades` |

`python3 heroic-categorize-tools.py <command> --help` gives examples for every
command. The rest of this page explains how it works and how to tune it.

---

# How it works

## The pipeline

```
Heroic library ──┐
Steam list     ──┼─→  Steam/SteamSpy  ──→  mapping.json  ──→  proposal.csv
favourites     ──┘       (cached)           (genres)             │
                                                                 │
                                        profiles.json ───────────┤
                                        (your taste)             │
                                                                 │
                                                                 ↓
                                                      proposal_profiles.csv
                                                                 ↓
                                                            Heroic config
```

`full` and `update` run the whole chain. Each arrow is also a command of its
own (`scan`, `profiles`, `apply`…) if you would rather review the CSV in a
spreadsheet before it touches Heroic — add `--no-apply` to stop there.

## The commands

**`full`** — everything, in order: backup, import Steam + favourites, look up
every title, apply `mapping.json`, add the categories from `profiles.json`,
write to Heroic. It *replaces* the categories from a previous run rather than
stacking onto them (keeping `Steam` and `Favorites`), so it is also the right
command after editing `mapping.json`. `--with-cache` skips the downloading.

**`update`** — the day-to-day one: new titles from your lists, games with no
category yet, and a retry of the titles Steam failed to find. Add `--all` to
re-examine everything.

**`combos`** — crosses two categories into one, `Already played+RPG`. See
[Crossed categories](#crossed-categories).

**`similar`** — ranks your library by tags shared with a game you liked:
`similar --anchor "Immortals of Aveum"`.

**`exclude`** — appends titles to `excluded-titles.txt`.

**`reset`**, **`cleanup`**, **`retry`** — remove categories, undo the imports,
forget failed lookups. All three ask first and back up first.

## `mapping.json`: what a game is

A Steam tag on the left, a Heroic category on the right. A tag can produce
several: `"survival horror": ["Horror", "Survival"]`.

The `_priorities` block decides who wins when a game matches more categories
than `--max-categories` allows. This matters more than it looks: SteamSpy
returns tags ordered by vote count, and the most-voted tags are always the
most generic ones. Without a priority table, `action` and `adventure` fill the
quota first and the informative tags further down the list — `local co-op`,
`jrpg`, `immersive sim` — never make it in. Higher number = more specific =
kept first. Official Steam genres are only consulted when no tag matched at
all, as a safety net for games with poor data.

A category holding 600 games out of 1200 is not a category. If one of yours
grows that big, split the tags feeding it rather than raising the cap.

## `profiles.json`: who it is for

Kept separate on purpose. `mapping.json` can only express OR — one tag is
enough to trigger a category — while a personal fit is a conjunction with
negations: *local co-op AND NOT punishing AND NOT depressing AND not one we
already finished*. Mixing the two would mean re-auditing a thousand
classifications every time a taste changes.

```jsonc
{
  "played_category": "Already played",         
  "exclude_title_files": ["excluded-titles.txt"], // set to null to switch it off

  "profiles": [
    {
      "category": "Couch Duo",
      "require_categories": ["Couch Co-op"],      // at least one mapping.json category
      "require_groups": [
        ["party game", "beat 'em up"],            // list -> at least one of these tags
        { "min": 3, "tags": ["fps", "magic"] }    // dict -> at least N of these tags
      ],
      "require_all": [],                          // every tag mandatory
      "exclude_any": ["souls-like", "sad"],       // one hit disqualifies
      "exclude_categories": ["Challenging"],
      "exclude_title_files": []                   // extra blocklist for this profile only
    }
  ]
}
```

Two levers when tuning, in order of usefulness:

1. **`exclude_any`** kills a whole family of false positives instantly.
2. **a group's `min`** turns a loose net into a shortlist — going from 1 to 3
   on a real library took one profile from 253 candidates down to 48.

A profile that returns nothing usually has a typo in a tag name: tags are the
SteamSpy ones, lowercase, exactly as they appear in the `top_tags` column of
`proposal.csv`.

## `excluded-titles.txt`: what you already played

One title per line, `#` starts a comment. No data source can fill this in for
you — Steam knows a game's tags, not whether you finished it — which is
exactly why it deserves its own file and its own command:

```bash
python3 heroic-categorize-tools.py exclude "Aven Colony" "ENDLESS Legend"
```

Those titles then drop out of every profile, and land in the **`Already
played`** category on the next run, where you can see them in Heroic and
notice what is still missing. The list works in both directions: it stops bad
suggestions, and it becomes a shelf of its own.

Matching ignores edition qualifiers, punctuation and trademark symbols
(`LEGO® The Hobbit™` = `lego the hobbit`), with a 0.90 similarity fallback.
Sequels are deliberately *not* collapsed: `Persona 5` does not block
`Persona 5 Royal`, so genuine variants must be listed.

## Crossed categories

`combos` builds the intersection of two categories as a third one. Fifty
categories means over a thousand possible pairs, so always pick:

```bash
# look first, write nothing
python3 heroic-categorize-tools.py combos proposal_profiles.csv --list

# everything that crosses one category
python3 heroic-categorize-tools.py combos proposal_profiles.csv \
    --with "Already played" --min-count 15 --apply

# exactly the ones you want
python3 heroic-categorize-tools.py combos proposal_profiles.csv \
    --pairs "For John+Investigation, Couch Duo+Platformer" --apply
```

`--prefix "0-"` puts the crossed categories at the top of Heroic's list.

## Optional lists

```bash
cp steam-games.EXAMPLE.txt steam-games.txt
cp favorites-not-on-pc.EXAMPLE.txt favorites-not-on-pc.txt
```

Steam games get a small launcher script, so they start from Heroic and Steam
does the work behind. Favourites are reference entries — console games,
wishlist items — that open their Steam page when clicked. Both are picked up
automatically by `full` and `update` when present, and skipped without
complaint when absent.

## Good to know

**`apply` adds, it never removes.** A game keeps the categories it already
had, which is what you want for a normal run but means changing `mapping.json`
leaves the old taxonomy alongside the new one. `full` clears first; elsewhere
use `apply --replace` or `reset`.

**Games with no category are missing data, not a missing rule.** They have no
Steam page, or SteamSpy has no tags for them. `retry` and `retry --tagless`
are the fix; a bigger `mapping.json` is not.

**Nothing is destructive.** `config.json`, `sideload_apps/library.json` and
`steam_cache.json` are copied to a timestamped `.bak-` file before any write.
Restore one by copying it back over the original with Heroic closed.

**Heroic must be closed.** It keeps its config in memory and rewrites it on
exit, silently undoing a run. The tool detects a live process and waits.

**Privacy.** `steam_cache.json` holds no credentials, no account ID and no
paths — just `title -> {appid, tags, genre}`. It does inventory your library,
as a public Steam profile would; same for your lists and `excluded-titles.txt`.
`.gitignore` excludes them all by default.
