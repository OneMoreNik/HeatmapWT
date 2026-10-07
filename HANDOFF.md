# Handoff

Read this first in a new session, or on a different machine. It says what
works, what does not, and what to do next. Format details are in
[docs/replay-format.md](docs/replay-format.md); the original goal is in
[docs/brief.html](docs/brief.html).

## The one-paragraph version

The goal is historical heatmaps of where each vehicle class moves on each War
Thunder ground map, shown as an overlay. Vehicle positions come from **playing
replays back in the client and recording what it draws on its own map**, which
is automated end to end: a script drives the replay browser, records at 20 Hz,
and a second script turns each recording into tracks, plots and heatmaps. The
alternative — **parsing server replays offline** — would scale to thousands of
battles and both teams, but is blocked on the position encoding in the packet
stream.

## Pipeline

```
replays/                     replays worth keeping, copied out of the game folder
  |  collector/sync_replays.py          copy them back into the game folder
  v
D:/Games/WarThunder/Replays
  |  collector/auto_replay.py           drive the client, record at 20 Hz
  v
data/live/<capture>/         map_obj.csv + map_info.json + source.json
  |  tools/process_recording.py         everything below, in one command
  v
  tracks.csv                 tools/build_tracks.py    one track per vehicle
  data/heatmaps/*.svg        tools/plot_tracks.py     routes on the map image
  data/heatmaps/*.png        heatmap/generate.py      dwell-weighted density
  data/heatmaps/*.html       tools/build_viewer.py    class switcher + opacity
```

Supporting data, fetched once per map and cached:

- `bin/wtlevel.exe` reads capture points, spawns and the battle area out of the
  **installed game**, so no calibration is ever needed.
- `collector/fetch_maps.py` downloads the map image and its size from
  wt-tools.app (62 maps indexed).
- `bin/wtresults.exe` reads the scoreboard out of any replay file: who played,
  their scores, and which team the recording player was on.

`tools/process_recording.py` calls both automatically when something is missing,
so a map that has never been seen before needs no preparation.

## Running it

```bash
# once per machine
python collector/sync_replays.py                 # repo replays -> game folder

# see what is there, read from the files, no game needed
python collector/auto_replay.py --list

# play and record (1x by default; add --speed 16 to go fast)
python collector/auto_replay.py --rows 0,1,2

# turn captures into heatmaps
python tools/process_recording.py data/live/*/ --skip-existing
```

Everything is the Python standard library plus Go for the replay parsers. No
pip installs. **Python 3.14 is required** — `compression.zstd` is used to read
replay packet streams.

## Setup on a new machine

1. **Go**: `winget install GoLang.Go`
2. **wrpl-inspector**, not vendored because it is AGPL:
   ```
   git clone https://github.com/maxsupermanhd/wrpl-inspector vendor/wrpl-inspector
   cd vendor/wrpl-inspector && git apply ../patches/*.patch
   ```
   The patches are required; without them it cannot read current replays at all.
3. Build each tool: `cd parser/wtcarve && go build -o ../../bin/wtcarve.exe .`,
   and the same for `parser/wtresults`, `parser/wtlevel`, `parser/wtprobe`.
4. If War Thunder is not at `D:/Games/WarThunder`, pass `--game` to
   `sync_replays.py` and `auto_replay.py`, and `-game` to `wtlevel.exe`.
5. Screen positions in `collector/ui_layout.json` were measured at 1920x1080.
   They scale proportionally, but the layout is not guaranteed to, so run
   `auto_replay.py --rows 0 --shots some/dir` once and look at the screenshots.

## Calibration, and how it was verified

Everything hangs off one chain, checked rather than assumed:

- The API's `x`/`y` are fractions of the map image. **The y fraction runs
  top-down while world Z runs bottom-up**, so Z is measured from `map_max`.
- Berlin's capture points then come out at Z 1119, 1140 and 1115 against the
  mission file's 1118.7, 1140.5 and 1115.2.
- Every stitched track starts within 5-86 m of a real spawn point.
- The map image covers a square whose **size** comes from wt-tools and whose
  **centre** is the mission's battle area. For Berlin Domination that predicts
  pixel positions for three capture points and both spawns to within a few
  pixels of the markers on the published image.

If alignment ever looks wrong, re-check in that order.

## Known traps

Each of these cost real time; none are obvious.

- **Connect to `127.0.0.1`, never `localhost`.** On Windows `localhost` resolves
  to `::1` first, the game listens only on IPv4, and every request pays the full
  connect timeout. This capped sampling at 0.5 Hz and looked exactly like the
  game throttling itself in the background. It is not: window focus makes no
  difference to the API.
- **Windows silently refuses `SetForegroundWindow`** from a process that does
  not already own the foreground, so clicks land in whatever window is in front.
  `focus_game()` attaches to the foreground window's input queue first and then
  verifies; every click re-checks and aborts rather than clicking blind.
- **Repeated clicks on the same pixel get swallowed.** Four presses of the speed
  button advanced it one step. The pointer is moved away and back between
  clicks so each is a fresh hover.
- **Esc does not leave a replay** — it switches the spectated player, which
  corrupts the followed track. A finished replay returns to the Replays dialog
  by itself.
- **The replay list is ordered by battle date, not filename.** An autosave is
  `#YYYY.MM.DD...` but a renamed replay drops the `#`, and `#` sorts before a
  digit, so sorting raw names puts a renamed replay first while the client lists
  it last.
- **The session id is at header offset `0x2DC`**, not `0x2E8`. The wrong offset
  gives an id-shaped number that silently 404s.
- **Replay packet streams are zstd**, not zlib. Game content BLKs are
  `SLIM_ZSTD_DICT` and need the archive's name map and dictionary.
- **Record with `--speed` matching the playback speed**, or every timestamp,
  speed and dwell time is wrong by that factor.
- **Missions name areas per difficulty.** Berlin has `_hardcore` and `_arcade`
  variants, Tunisia only `_arcade`. Look for the best available rather than
  assuming `_hardcore`, or most maps yield no capture points at all.
- **Map bounds can change mid-battle.** When a spectated player takes a plane,
  `map_info` switches to the air map (`map_min` around `[-28672, -45056]`).
  The recorder splits on `map_generation`, which keeps the ground part clean;
  without that split those frames would be converted at the wrong scale.

## What the playback method cannot do

Established from several recordings and roughly 100,000 rows, not inferred:

- **Enemy vehicles never appear.** Every `ground_model` the API reports is on
  the author's own team. Red shows up only for enemy spawn zones, airfields and
  contested capture zones. The client draws enemy markers on screen but does not
  expose them over HTTP, almost certainly deliberately.
- **One playback per battle.** Even at 16x that is a minute each, and the client
  has to be running and in the foreground.

So this path is right for geometry, for studying a particular player, and for
building up a map at a steady rate. It is the wrong path for thousands of
battles.

## Does it matter whose eyes we watch through?

**For the heatmap, no — reasoned, not yet tested.** A client replay contains
only the packet stream the recording player's client received. Teammate
positions are in there continuously, which is how their minimap worked; enemy
positions never were. Switching the camera cannot reveal units the client never
received, so the recorded set should be the same whichever teammate is watched.

**For following one player, yes.** The spectated vehicle is the one drawn amber,
and it is the only vehicle the API identifies. Every other track is anonymous.
So the best player's route is already in the recordings — just unlabelled.

**Untested:** whether an enemy can be spectated at all, and what the map shows
if so. Probably very little, since the data is not in the file.

## Following a chosen player

`--follow` takes `author` (the default, no clicking needed), `best` for the
highest scorer on the author's team, or part of a name:

```bash
python collector/auto_replay.py --rows 7 --follow best
python collector/auto_replay.py --rows 7 --follow Ph4nt0m
```

The row is computed offline. The client's in-replay player list is that team's
players **in the order the results block stores them**, which `wtresults`
reports as `slot`. Verified against a Tunisia battle: all 16 names in the same
order.

Two caveats:

- The order only settles once every player has joined. Early in a replay it
  differs, so the click waits `player_list_settle_s` (8 s) first.
- **`player_list` geometry in `ui_layout.json` is unverified.** `first_y` and
  `row_height` were read off a compressed screenshot and have never been
  clicked. Run once with `--shots` and check before trusting `--follow`.
- `best` deliberately means best on the **author's team**. The highest scorer
  overall may be an enemy, who probably cannot be followed at all.

## The blocker: positions in server replays

Read [docs/replay-format.md](docs/replay-format.md) before attacking this.

- The per-vehicle update stream is found: **packet type 4, payload length 63**,
  marker `cc f0 35 00 fe 01` at `payload[5:11]`, varint entity id at
  `payload[2:]`, about 5.3 updates per vehicle per second. It is the only
  per-vehicle stream in the file.
- Layout: unit quaternion at +11, a float32 triple at +27, a unit vector at
  +39, then component-indexed values.
- The triple at +27 is **not** the position. It splits the fleet 14/12 by team
  correctly, but vehicles starting within 200 m of each other spread over
  1400 m, single steps reach 1143 m, and its magnitude is near-identical across
  every vehicle at spawn. That is a direction, not a place.
- Scanning every byte offset for a float triple inside Berlin's real world box
  finds nothing consistent, so positions are quantised or delta-encoded.

**There is now an answer key.** Several recordings hold real tracks for known
battles, on maps aligned to within metres — for example
`data/live/*-20261006-192150/tracks.csv` against session `127b00490002868f`.
Any candidate decoding either reproduces those tracks or it does not, which
was the missing piece before.

Also unresolved, probably the same root cause: `ecshashes.json` predates this
game version, so construction messages decode zero components and no track can
be tied to a vehicle model or player. Component ids are **FNV-1a 32-bit** of
the component name — verified against all 6647 known pairs, with no other
candidate hash matching one. The types would come from the game's template
BLKs, which are now readable.

## Next steps, in order

1. **Volume.** Everything rests on one battle per map. Several battles on the
   same map and layout will show whether the binning and smoothing hold up.
   `heatmap/generate.py` already takes several `tracks.csv` files at once.
2. **Verify `--follow`** against a real run with `--shots`, and settle whether
   an enemy can be spectated.
3. **Decode the type-4 position encoding**, checked against the answer key.
   That unblocks batch processing, both teams, and vehicle class from the
   replay rather than from map icons.
4. **Storage.** A battle is a few MB of CSV. Past a few dozen, move to Parquet
   or DuckDB as the brief suggests.
5. **The overlay** (Ctrl+Q large map, Ctrl+E minimap) — last, and only once the
   heatmaps are worth overlaying.
