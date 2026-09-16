# Heroic Games Launcher — automatic categorisation

Automatically categorise your [Heroic Games Launcher](https://heroicgameslauncher.com/)
(2.x) library without IGDB. Genres and tags come from the public Steam Store
API (no key, no account) and then from SteamSpy (no key, no account), which
between them cover nearly every PC game — even the ones you bought on
Epic/GOG/Amazon, and even the ones you own only on Steam or nowhere at all.

Works on **Linux, Windows and macOS**. Python 3 standard library only, no
dependencies to install.

## Overview

Two scripts, one workflow, always in two steps (scan/import → review → apply)
so that **nothing is ever written to your configuration without you reviewing
it first**:

| Script | Role |
|---|---|
| `heroic_categorize.py` | Categorises what Heroic already knows natively: Epic (`legendary`), GOG (`gog`), Amazon (`nile`). Commands: `scan`, `apply`, `combos`, `similar`. |
| `heroic_import_external.py` | Creates the games Heroic cannot see at all, as *sideload* entries: your **Steam** library, and games you have played **without owning them on PC**. Imports `heroic_categorize.py` as a module — no duplicated logic. Commands: `steam`, `favorites`. |

The pattern is the same everywhere: one command **proposes** (a
`proposal*.csv` you can review and fix in Excel/LibreOffice), another one
**applies** (`heroic_categorize.py apply ...`) once you are happy.

### Repository layout

- `heroic_categorize.py` — Epic/GOG/Amazon categorisation
- `heroic_import_external.py` — Steam import + not-owned-on-PC import
- `mapping.json` — Steam tag → Heroic category table (edit it freely, see [Heroic only](#heroic-only-epic-gog-amazon))
- `steam-games.example.txt` — template for your Steam library list
- `favorites-not-on-pc.example.txt` — template for games you own on no PC store
- `.gitignore` — keeps your personal lists and cache out of the repo

Generated at runtime, not tracked:

- `steam_cache.json` — cached Steam/SteamSpy responses (keep it between runs so nothing is re-downloaded)
- `proposal.csv`, `proposal_steam.csv`, `proposal_favorites.csv`, `proposal_combos.csv`, `similar.csv`

### Privacy note

`steam_cache.json` contains **no credentials, no account ID, no file paths**
— only `title → {appid, tags, genre}`. Nothing in it can be used to access
anything of yours.

It does, however, list every game title it has seen, which effectively is an
inventory of your library. Same for `steam-games.txt` and
`favorites-not-on-pc.txt`. That is personal information, not a security risk:
publishing it is roughly like making your Steam profile public. The shipped
`.gitignore` excludes all three by default; delete those lines if you do not
mind sharing, or commit a trimmed cache if you want to spare other users some
API calls.

### Requirements

```bash
python3 --version    # any Python 3
```

Put every file in the **same folder** (e.g. `~/heroic-tools/` or
`C:\heroic-tools\`).

On Windows, replace `python3` with `python` in every command below.

---

## Quickstart

Always close Heroic before writing anything. Closing the window is not
enough — Heroic usually stays alive in the system tray, keeps the old config
in memory, and overwrites your changes on its next internal save.

```bash
# Linux / macOS
ps aux | grep -i heroic      # any line other than grep itself?
pkill -f -i heroic
```

```powershell
# Windows
tasklist /FI "IMAGENAME eq Heroic.exe"
taskkill /F /IM Heroic.exe /T
```

(The scripts refuse to run when they detect a live Heroic process, but check
by hand anyway.)

### Full init from scratch

Categorise everything for the first time: the native stores, then Steam, then
the games you own nowhere.

```bash
cd ~/heroic-tools

# 1. What Heroic already knows (Epic/GOG/Amazon)
python3 heroic_categorize.py scan --max-categories 5
#   -> review/fix proposal.csv, then:
python3 heroic_categorize.py apply proposal.csv

# 2. Your Steam games (invisible to Heroic)
cp steam-games.example.txt steam-games.txt   # then fill it with your library
python3 heroic_import_external.py steam --list steam-games.txt --dry-run
python3 heroic_import_external.py steam --list steam-games.txt
#   -> review/fix proposal_steam.csv, then:
python3 heroic_categorize.py apply proposal_steam.csv

# 3. Your favourites, owned on no PC store
cp favorites-not-on-pc.example.txt favorites-not-on-pc.txt
python3 heroic_import_external.py favorites --list favorites-not-on-pc.txt \
    --exclude-list steam-games.txt
#   -> review/fix proposal_favorites.csv, then:
python3 heroic_categorize.py apply proposal_favorites.csv
```

Restart Heroic: everything is categorised. Options and safeguards for each
step are in [Detailed](#detailed).

### Update files and categories

Coming back later: only process what changed. Everything is idempotent —
re-running with nothing new does nothing (`steam`/`favorites`) or re-scans
nothing (`--only-uncategorized`).

```bash
cd ~/heroic-tools

# New Epic/GOG/Amazon games only
python3 heroic_categorize.py scan --only-uncategorized --max-categories 5
python3 heroic_categorize.py apply proposal.csv

# steam-games.txt updated? Already-imported titles are skipped automatically.
python3 heroic_import_external.py steam --list steam-games.txt
python3 heroic_categorize.py apply proposal_steam.csv

# favorites-not-on-pc.txt extended? Same, only new titles are processed.
python3 heroic_import_external.py favorites --list favorites-not-on-pc.txt \
    --exclude-list steam-games.txt
python3 heroic_categorize.py apply proposal_favorites.csv
```

---

## Detailed

### Heroic only (Epic, GOG, Amazon)

Handled by `heroic_categorize.py`, for what Heroic manages natively.

**Where the config lives.** The script finds it on its own:

| OS | Path |
|---|---|
| Linux | `~/.config/heroic/` |
| Windows | `%APPDATA%\heroic\` |
| macOS | `~/Library/Application Support/heroic/` |

Inside that folder:

```
store/config.json                       (categories)
store_cache/legendary_library.json      (Epic games)
store_cache/gog_library.json            (GOG games)
store_cache/nile_library.json           (Amazon games)
sideload_apps/library.json              (manually added games)
```

You only need `--heroic-dir` if your config is somewhere else (custom
profile, Flatpak, portable install):

```bash
python3 heroic_categorize.py --heroic-dir ~/.var/app/com.heroicgameslauncher.hgl/config/heroic scan
```

(that is the usual Flatpak path — check with `ls` if the normal one comes up
empty).

**scan.** Reads the Heroic library, queries Steam/SteamSpy, proposes a
category per game, writes `proposal.csv`.

```bash
python3 heroic_categorize.py scan --only-uncategorized --max-categories 5
```

`--only-uncategorized` skips every game already sitting in a category — handy
to process only what you added since last time. `--limit N` runs on a small
sample for testing. `--lang` / `--cc` change the Steam locale (defaults:
`english` / `us`).

**Review.** Open `proposal.csv` (Excel/LibreOffice Calc). The `category`
column can hold several categories separated by `; ` (e.g. `Action; RPG`).
Fix or complete by hand, then save keeping the CSV format.

**apply.** With Heroic closed:

```bash
python3 heroic_categorize.py apply proposal.csv
```

The previous `config.json` is backed up automatically
(`config.json.bak-YYYYMMDD-HHMMSS`), so nothing is ever lost.

**Going further — combined categories and "games like X".**

```bash
# "Action+RPG" style categories (needs at least 3 games to be created)
python3 heroic_categorize.py combos proposal.csv --min-count 3
python3 heroic_categorize.py apply proposal_combos.csv

# Games resembling one specific game in your library
python3 heroic_categorize.py similar --anchor "Aven Colony" --category "Like Aven Colony" --min-shared 3
python3 heroic_categorize.py apply similar.csv

# Games with some tags but not others (e.g. narrative FPS, no horror)
python3 heroic_categorize.py similar --require "fps,story rich" --require-all --exclude "horror" --category "Narrative FPS"
python3 heroic_categorize.py apply similar.csv
```

**`mapping.json`.** The Steam tag → category table. An entry can point at one
category (`"horror": "Horror"`) or several (`"survival horror": ["Horror",
"Survival"]`). Edit it directly — you never need to touch the Python code.
Categories are matched against the most-voted SteamSpy tags first, then the
official Steam genres.

**Reminders.**

- Heroic uses `app_name_runner` as the identifier inside categories (e.g.
  `dc07b9ead8214591b8df6d2546d2a0e3_legendary`), not `app_name` alone — the
  script handles that through the CSV's `heroic_id` column.
- If your config reverts after restarting Heroic, the Heroic process was not
  fully closed (see [Quickstart](#quickstart)).
- Every command accepts `-h`: `python3 heroic_categorize.py scan -h`.

### Add Steam games

Heroic only reads Epic/GOG/Amazon, so games you own on **Steam** (KOTOR 2,
Skyrim, Rise of Nations…) are invisible to it.
`heroic_import_external.py steam` creates them as *sideload* entries — the
same mechanism as Heroic's own "Add Game" button — so they can be categorised
and launched from Heroic.

```bash
python3 heroic_import_external.py steam --list steam-games.txt --dry-run
python3 heroic_import_external.py steam --list steam-games.txt
python3 heroic_categorize.py apply proposal_steam.csv
```

What it does:

- compares `steam-games.txt` against what Heroic already knows and keeps only
  the missing titles;
- filters out demos, betas, public tests, editors, DLC and packs along the way
  (disable with `--no-default-skip`, extend with `--skip "pattern"`);
- looks up each game's Steam appid → official artwork (`header.jpg` +
  `library_600x900.jpg`) and SteamSpy tags;
- writes a launcher script per game in `~/heroic-steam-launchers/`, handing
  the `steam://rungameid/<appid>` URL to the OS — a `.sh` on Linux/macOS, a
  `.cmd` on Windows. **The games really do launch from Heroic**; Steam does
  the work behind the scenes;
- applies `mapping.json` to propose categories, plus a `Steam` category added
  to every entry (change it with `--extra-category`).

Steam must be installed and its URL protocol registered — which it is by
default on all three platforms.

Useful options: `--no-launcher` (reference-only, non-launchable entries),
`--keep-unmatched` (keep titles Steam cannot find), `--launchers-dir`
(different folder for the scripts), `--limit N` (testing).

**Producing `steam-games.txt`.** One title per line, `#` for comments. Any
Steam library exporter works; so does copying the list out of the Steam
client. Exact store spelling helps the appid lookup, but the matcher is
forgiving.

### Add specific other games

Games you have played (console, an old PC, a friend's machine…) but own on
**no PC store** — invisible both to Heroic and to the `steam` subcommand.
`heroic_import_external.py favorites` creates them as reference entries.

```bash
python3 heroic_import_external.py favorites \
    --list favorites-not-on-pc.txt \
    --exclude-list steam-games.txt \
    --exclude-list epic-games.txt --exclude-list gog-games.txt
python3 heroic_categorize.py apply proposal_favorites.csv
```

`--exclude-list` is a safety net, repeatable: if a title in your favourites
list turns out to live in one of your libraries after all, it is skipped
rather than duplicated. It is optional — Heroic's own library is always
checked regardless.

These entries are created in **browser** mode: `install.platform = "Browser"`
and a `browserUrl` pointing at the Steam store page. Clicking one opens the
page in Heroic's built-in browser, which is handy for checking a price or a
sale. They all get a `Favorites` category (change it with
`--extra-category`).

A title with no Steam page is still imported, just without artwork or an
automatic category — fill the `category` cell in by hand before `apply`.

### Clean up / reset

**Restore a config after a mishap.** Every `apply` writes a timestamped
backup first:

```bash
ls ~/.config/heroic/store/config.json.bak-*
cp ~/.config/heroic/store/config.json.bak-YYYYMMDD-HHMMSS ~/.config/heroic/store/config.json
```

Same for `sideload_apps/library.json.bak-...`, written by
`heroic_import_external.py`. On Windows, the same files live under
`%APPDATA%\heroic\` and copy back with `copy`.

**Start a clean scan** without touching the categories already applied in
Heroic: just delete the generated CSVs (`proposal.csv`, `proposal_steam.csv`,
`proposal_favorites.csv`, `proposal_combos.csv`, `similar.csv`) and re-run
`scan` / `steam` / `favorites`. Keep `steam_cache.json` so nothing is
re-downloaded.

**Remove the imported Steam / favourites entries.** They are the only ones
with `"runner": "sideload"` and a recognisable description. Save this as
`cleanup.py` next to the other scripts and run it with Heroic closed:

```python
import json, os, platform

if platform.system() == "Windows":
    base = os.path.join(os.environ["APPDATA"], "heroic")
elif platform.system() == "Darwin":
    base = os.path.expanduser("~/Library/Application Support/heroic")
else:
    base = os.path.expanduser("~/.config/heroic")

path = os.path.join(base, "sideload_apps", "library.json")
data = json.load(open(path, encoding="utf-8"))
before = len(data["games"])
data["games"] = [
    g for g in data["games"]
    if "imported into Heroic" not in (g.get("description") or "")
    and "Reference entry" not in (g.get("description") or "")
]
json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"{before - len(data['games'])} entries removed.")
```

Categories in `store/config.json` that end up pointing at `heroic_id` values
which no longer exist are harmless — Heroic ignores them silently. The
generated launcher scripts in `~/heroic-steam-launchers/` can be deleted by
hand.

## License

MIT. Do whatever you like with it.
