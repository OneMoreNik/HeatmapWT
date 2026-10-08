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
  wt-tools.app. **It publishes 62 maps, which is not all of them** — checked
  against the page itself; fortress (White Rock Fortress), rheinland, alps,
  guadalcanal and moscow_serpuhov are among the missing, and its published size
  is not always right (see Known traps). A missing one can be filled in by hand
  with `tools/import_map_image.py`. A map image is
  decoration, not a dependency: the client reports the playable square as
  `grid_size` in every recording's `map_info.json`, and its centre matches the
  mission's battle area, so the geometry survives without one.
- **The game's map name and wt-tools' key often differ.** The game says
  `hurtgen` where wt-tools says `battle_of_hurtgen_forest`, `vietnam_hills`
  against `vietnam`, `eastern_europe_02` against `eastern_europe`.
  `resolve_map_key()` in `collector/fetch_maps.py` handles this by trimming
  numbering, trying shorter prefixes and then matching on shared words, with a
  short alias table for the ones that cannot be derived. Add to `MAP_ALIASES`
  when a new one turns up — and add one even when the word match happens to
  work, as `container_port` -> `cargo_port` did only because `port_novorossiysk`
  shares the same single word and lost a tie.
- `bin/wtresults.exe` reads the scoreboard out of any replay file: who played,
  their scores, and which team the recording player was on.
- `tools/import_map_image.py` makes a background for a map wt-tools does not
  publish, out of a minimap screenshot. A screenshot says nothing about what
  world square it covers, so the capture points supply it: the mission gives
  their world positions, the minimap draws each as a small light disc with a
  letter, and three known points fix scale and offset. The fit is then checked
  rather than trusted — it has to put every cap back within 25 m, and a mirrored
  match is rejected on the sign of the scale. White Rock Fortress came out at
  2.6725 m/px, reproducing all three caps to within 0.5 m, and its square landed
  within 4 px of the grid the client reported. Two traps: the letter inside a
  disc breaks the ring into separate blobs, which have to be rejoined before
  measuring, and judging the fit by the spread of the pairwise scale estimates
  is misleading, because two caps 55 px apart turn half a pixel of error into
  1% of scale. Judge it on the residual in metres.

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

# everything from "I stopped playing" to "the overlay has new heatmaps"
python tools/update.py

# or the same three steps separately
python tools/backfill_source.py data/live/*/
python tools/process_recording.py data/live/*/ --skip-existing

# what has been recorded, and one heatmap per map+layout across every battle
python tools/build_map.py --list
python tools/build_map.py
python tools/build_map.py --by-map       # mixes layouts, keeps the terrain

# give an unpublished map a background, from a minimap screenshot
python tools/import_map_image.py maps/<shot>.png --recording data/live/<capture>
```

Everything is the Python standard library plus Go for the replay parsers. No
pip installs. **Python 3.14 is required** — `compression.zstd` is used to read
replay packet streams.

## Setup on a new machine

1. **Go**: `winget install GoLang.Go`
2. **wrpl-inspector**, not vendored because it is AGPL:
   ```
   git clone https://github.com/maxsupermanhd/wrpl-inspector vendor/wrpl-inspector
   cd vendor/wrpl-inspector; git apply ../patches/*.patch
   ```
   The patches are required; without them it cannot read current replays at all.
3. Build each tool: `cd parser/wtcarve; go build -o ../../bin/wtcarve.exe .`,
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
- **A battle type splits on the LAST underscore.** `soviet_range_Dom` is the
  map `soviet_range` with layout `Dom`, not `soviet` with `range_dom`.
- **Missions name their areas inconsistently, and it is worse than it looks.**
  Berlin has `dom_capture_area_01_hardcore` and `dom_battle_area_hardcore`;
  Tunisia defines only `_arcade` variants; Poland writes `battlearea` as one
  word, uses `captureZone` and `tankSpawn` in camel case, marks Realistic with
  `Rb` rather than `hardcore`, and prefixes *every* area with `briefing_`, so
  excluding those leaves nothing at all. `heatmapwt/layout.py` normalises names
  and picks the best difficulty variant with a fall back; use it rather than
  matching names directly.
- **Map bounds can change mid-battle.** When a spectated player takes a plane,
  `map_info` switches to the air map (`map_min` around `[-28672, -45056]`).
  The recorder splits on `map_generation`, which keeps the ground part clean;
  without that split those frames would be converted at the wrong scale.
- **One battle can land in several recording folders, and missing that loses
  most of it.** `map_generation` also ticks *within* a battle, and the recorder
  splits on it. Test-Site 2271 came out as 8.8 minutes in one folder followed by
  13.9 in the next, and the heatmap built from the first alone covered a third
  of the battle while looking perfectly healthy. `battle_group()` in
  `tools/process_recording.py` rejoins folders that describe the same grid and
  start where the previous one stopped; `build_tracks.py` takes several
  directories and offsets each by the gap between their names. Verified on that
  battle: 5 tracks cross the seam, the largest jump across it is 0.3 m.
- **`grid_size` beats the published size only when `grid_steps` agrees with
  `tile_size`.** The client's grid is usually the battle area exactly, and is
  sometimes righter than wt-tools:

  | map | wt-tools size / tile | client grid / steps |
  |---|---|---|
  | cargo_port | 1800 / **250** | 1800 / **250** |
  | berlin | 1300 / **180** | 1300 / **180** |
  | test-site_2271 | 1600 / **225** | 1700 / **225** |
  | finland | 1700 / **225** | 2048 / **275** |

  Where the tile matches, both are measuring the same grid, so a disagreement
  over its size is wt-tools being wrong — Test-Site 2271 really is 1700 m, which
  its own image confirms. Where the tile differs, the client is describing
  something else: Finland reports the whole 2048 m map, with `grid_zero` on the
  map's own corner, while its image covers 1700 m. Taking that literally
  stretches the image by 20% and skews every track with it. `client_grid_size()`
  in `tools/process_recording.py` checks the tile first, then falls back to
  rejecting the whole-map shape when nothing is published to compare against.
- **One battle covers one side of the map, and only one.** A live capture sees
  the recording player's own team and nothing else, so a map played once holds
  that team's movement and nothing of the other half -- which reads as quiet
  ground rather than as ground nobody watched. Of 22 layouts recorded so far,
  only Israel has been played from both sides. Which side cannot be read from
  the results: the team numbers there do not line up with the mission's t1 and
  t2, and Israel has `authorTeam` 1 spawning at t2 and `authorTeam` 2 at t1. It
  is read off the ground instead -- every track's first position is a spawn, and
  they agree almost perfectly, 100% of tracks in most battles. `build_map.py`
  records it and the viewer says so on the page.
- **A map's square belongs to the map, not to each capture of it.** Captures
  grouped by battle type are the same map and layout by construction, and track
  positions are converted with `map_min`/`map_max`, never the grid -- so a
  capture that opened on a transient grid still holds good tracks. Checking each
  member's grid against the reference threw a real Berlin Conquest-2 battle out
  of its own heatmap. The share of samples actually drawn is the guard instead,
  and it is reported on every build.
- **Take a combined map's geometry from the most authoritative capture, which
  need not be one of the battles in it.** Deduplicating by session keeps the
  capture with the most data; for Berlin Conquest-2 that is one which opened on
  a transient 1700 m grid, and building on it lost Berlin's map image. Any
  capture of the same battle type describes the same ground, so one whose grid
  the client vouched for is preferred.
- **The published map can be the wrong map.** `sinai_02` trims to `sinai`,
  which exists and covers 1500 m, while the client reports 2400 m: not a
  disagreement about one square but two different places. Drawing on it put 84%
  of that battle off the edge, and `normandy_02` and
  `lazzaro_italy_new_city` did the same -- the last matching `sun_city` on the
  single shared word "city". A real grid no published mode comes close to now
  means the name matched something else, and the image is dropped; one shared
  word, or a filler word like "city" or "port", is no longer a match at all.
- **Only rebuild the maps whose battles changed.** `build_map.py` redrew all
  nineteen layouts on every run, 219 s, growing with the collection -- while one
  battle changes exactly one map. A heatmap newer than every track that went
  into it is already right, so it is left alone: a run after a battle went from
  219 s to 13, and one with nothing new to 0.3. `--rebuild` forces the old
  behaviour, which is what to use after changing how heatmaps are drawn, since
  that changes the output without touching any input.
- **Windows PowerShell has no `&&`.** It is not a statement separator there, so
  any instruction that chains commands with it fails on the machine this runs
  on. `tools/update.py` exists so the normal path is one command rather than a
  chain; where steps must be shown separately, they go on separate lines.
- **A capture made while playing has to be paired with its replay by time.**
  `auto_replay.py` names its folders after the replay, but a capture made while
  actually playing has no such name, and every one had to be pointed at its
  replay by hand. A replay's file is written when the battle ends and its header
  gives the battle's length, so the battle started at (write time - length), and
  a recording of it starts within seconds: across eleven battles the right
  replay was never more than 8 s out and the nearest wrong one never closer than
  164 s. Two traps. The modification time has to come from the *original* copy --
  copying a replay into `replays/` gives the copy a new one, which was enough to
  pair 6 of 11 wrongly. And a match that is not clearly better than the
  runner-up is refused rather than guessed, because a wrong pairing silently
  mislabels a map. Verified by deleting all eleven hand-written `source.json`
  files and reproducing them exactly.
- **Count battles by `sessionId`, not by capture folder.** The same battle can
  be recorded more than once -- replaying a replay to test a change does exactly
  that. Two Berlin Conquest-2 captures on disk are one battle, session
  `127b00490002868f`, and left in it would count twice towards the confidence
  the viewer reports and weigh twice as heavily in the density. `build_map.py`
  keeps the longest capture of each session.
- **Take the battle area's centre from the client, not from the mission's
  names.** A mission offers several battle areas and nothing in the naming says
  which one is live. Berlin's client grid matches `dom_battle_area_hardcore` to
  0.1 m; Volokolamsk's matches the plain `briefing_battlearea`, and its
  `_hardcore` one describes a square **1152 m away**, which on a 1400 m map is a
  different part of the world. Every recording's `grid_zero` and `grid_size`
  give the answer exactly, so `process_recording.py` passes `--centre` to the
  renderers and `pick_areas(..., near=)` chooses the capture and spawn variants
  nearest that point. Checked on all seven battles: every centre lands on the
  mission's own battle area to the centimetre, and track starts sit a median
  0-91 m from a real spawn.
- **A battle area need not be square.** Hurtgen's `grid_size` is
  `[1550.0, 1800.0]`. Everything downstream draws a square, so the longer side
  is used -- which is also what wt-tools publishes for it -- and the centre is
  taken from each axis separately. Reading only `grid_size[0]`, as the code did,
  cut 250 m off the north-south extent and left 18% of that battle's tracks
  outside the picture.
- **"Prefer real areas over `briefing_` ones" is not safe on its own.**
  Hurtgen's only non-briefing spawn areas are `teamB_artillery_spawn_*`, 3.3 km
  outside the battle area, so that preference picked artillery positions over
  the tank spawns and put every track start a median 3863 m from a "spawn". When
  the battle's centre is known, proximity decides the briefing split as well as
  the difficulty one.
- **A mission may define no battle area at all.** Middle East has one capture
  zone and two respawns, named `resp01` rather than anything containing "spawn".
  Their midpoint is a rough guess -- good enough to draw a grid around, not to
  align a published image against -- so when there is no battle area the image
  is dropped and the client's own square is drawn verbatim instead.
- **`Airdefence` is not a player SPAA.** It is the static base AA. On Fortress
  the client reported ten of them in a single 0.8 s burst, at fixed positions
  250-420 m outside the battle area, while the six real player SPAA came through
  as `SPAA`. Counting both put ten phantom emplacements in that map's SPAA
  layer. Leaving a battle makes the client emit one frame of a different entity
  set, so `MIN_TRACK_SECONDS` drops anything under 2 s as well — at 20 Hz a
  0.8 s burst is 17 samples, easily enough to pass a sample-count threshold.

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

**No. Tested, not assumed.** The same Berlin Conquest 2 replay was recorded
three times, watching three different players:

| Watched | Tracks | Sides recorded |
|---|---|---|
| ProfileLoky (the author) | 49 | all own team |
| Seva172 (same team) | 46 | all own team |
| the top enemy scorer | — | all own team, still no red |

A client replay holds only the packet stream the recording player's client
received: team mates continuously, enemies never. Switching the camera cannot
reveal units that were never in the file. Selecting an enemy *works* — the row
highlights — but the client says "местонахождение временно неизвестно", its
location is temporarily unknown, and the map keeps showing the author's team.

So for heatmaps, watch whoever is convenient. The author is the default
because it needs no clicking at all.

**For following one player it does matter**, because the spectated vehicle is
the only one the API identifies, drawn amber. Every other track is anonymous.
The best player's route is therefore already in any recording of that battle —
just unlabelled until you spectate them.

## Following a chosen player

`--follow` takes:

- `author` — the default; the replay opens on them, so nothing is clicked
- `best` — highest scorer on the author's team
- `overall` — highest scorer in the battle, even if that is an enemy
- any substring of a player name

```bash
python collector/auto_replay.py --replay 19.21.50 --follow best
python collector/auto_replay.py --replay 19.21.50 --follow Ph4nt0m
```

**Prefer `--replay` over `--rows`.** Row numbers shift every time a battle is
played, so naming the file is the safer way to pick one.

How the row is found, with no reading of the screen: the client's in-replay
player list is that team's players **in the order the results block stores
them**, which `wtresults` reports as `slot`. Own team runs down the left edge,
the opposing team down the right.

The one trap: players who have not joined yet are **absent from the list**, so
everyone below them shifts up. A first attempt clicked AlucarDracula instead of
Seva172 for exactly this reason. The speed is therefore set first and the click
waits `player_list_settle_battle_s` (150 battle seconds, which is about 9 s of
wall clock at 16x) for the roster to fill. Verified by screenshot afterwards:
the target highlights amber with its name in a tooltip.

## Tested

Run after any change to the capture or analysis path:

```bash
python collector/auto_replay.py --replay <name> --follow best --speed 16 --shots shots/
python tools/process_recording.py data/live/<capture>
```

What has been checked end to end, on eleven battles across ten maps:

- Clicks land: the replay list, the watch button, the speed control and the
  player row were each confirmed by screenshot.
- The speed reaches 16x and the label changes; a missed button is reported.
- `--follow` highlights the intended player, verified by the tooltip.
- A map never seen before (Poland) has its layout extracted from the installed
  game and its image fetched, with no preparation.
- Alignment holds: 100% of Poland, Tunisia and soviet_range samples fall inside
  the map square, 95.6% on Berlin, and tracks start a median 24-53 m from a
  real spawn.
- The grid fallback is exact. On soviet_range, which wt-tools does not publish,
  the mission's battle-area centre and the client's grid centre are the same
  point, (2004, 2023), derived independently.
- A live battle records and processes: 56 tracks and 106,838 samples from one
  battle at 20 Hz in real time.
- The automation refuses to start while a battle is live.
- Three consecutive live battles capture and process unattended, with no
  replay playback at all: Cargo Port 23 tracks / 37,838 samples, Finland 43 /
  120,761, Fortress 23 / 59,268. The recorder had been left running and each
  battle landed in its own folder.
- Folders are rejoined correctly: Test-Site 2271's two folders merge into one
  22.4 minute timeline, 63 tracks and 198,339 samples, with 5 tracks crossing
  the seam and a largest jump across it of 0.3 m.
- Out-of-square samples are dropped, not clamped to the edge: `Grid.add` returns
  False, so Fortress's 170 off-map staging samples vanish rather than piling up
  along the south boundary.
- Every battle's square now holds 100% of its own samples, across all seven live
  captures and four maps whose geometry had to be chosen rather than read.
- A map with no published image and no battle area still works: Middle East
  draws on the client's grid alone, with 100% of samples inside and track starts
  a median 32 m from a respawn.

## Recording a live battle

`collector/sample_map.py` is the recorder: it polls the client's local API and
writes down every unit drawn on the map. **It is normally not run by hand** —
`auto_replay.py` starts and stops it around each replay. Running it directly is
for capturing a live battle as you play it:

```bash
python collector/sample_map.py --hz 20 --label my-battle
```

It records nothing until a map exists, so start it, then start the battle. It
prints a line every 10 seconds while waiting, because silence looks identical
to a hang. When the battle ends it writes the folder and goes back to waiting;
stop it with Ctrl-C.

Afterwards, attach a source.json with `tools/backfill_source.py` and run
`process_recording.py`. The replay the battle produced has to exist for that,
so let the client autosave it first.

**Never run `auto_replay.py` while a battle is live.** It automates the replay
browser in the menus, which is not gameplay; pointing it at a live battle would
be. It checks `map_info` and refuses to start if one is running, and every
click re-checks that the game is the foreground window.

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

## Using it

`process_recording.py` makes one heatmap per battle, which is what to look at
when checking a capture worked. `build_map.py` makes one per map and layout out
of every battle recorded there, which is what to look at before playing:

```bash
python tools/build_map.py --list          # inventory, and which maps repeat
python tools/build_map.py                 # one heatmap per map+layout
python tools/build_map.py berlin_Conq2    # just one
python tools/build_map.py --by-map        # every layout of a map together
```

The layout is the unit, not the map: `berlin_Dom` and `berlin_Conq2` have
different capture points and a square 118 m apart, so stacking them averages two
different battles. `--by-map` does it anyway, which mixes the objectives but
keeps the terrain -- the roads people take, the ridges they stop behind -- and
reports what share of the samples fit the square so a bad mix is visible rather
than silent. Berlin across both layouts draws 98.5%.

One battle per layout is not a heatmap, it is a single battle drawn in colour.
Five or six on the same map and mode is where a hot spot starts to mean "people
go here" rather than "someone went here once".

## The overlay

`overlay/overlay.py` puts those heatmaps on the screen over the game.

```bash
python overlay/overlay.py --calibrate map       # once, per screen layout
python overlay/overlay.py --calibrate minimap
python overlay/overlay.py                       # then leave it running
```

    Ctrl+Q   over the big pre-battle tactical map
    Ctrl+E   over the minimap
    Ctrl+R   next vehicle class
    Ctrl+D   hide

It picks the map itself, from `map_info.json`.

**Identify the map by the whole of `map_info`, never by where its square sits.**
The first version matched on the centre and size of the playable square within a
400 m radius, and put **Attica on Cargo Port's heatmap** -- their battle areas
are close enough together -- then drew it with full confidence. A near miss is
not a map. `grid_zero`, both grid sides, `grid_steps` and the map extent
together are distinctive: across every recording made so far no two different
maps share them, and the only collisions are between layouts of one map. The
numbers come from the same source on both sides, so they are compared for
equality, and anything unrecognised shows nothing at all.

`build_map.py` writes those fingerprints to `<prefix>.index.json` beside each
combined heatmap. Two details matter:

- **Every capture contributes a fingerprint, not just the deduplicated one.**
  Deduplicating by session is right for counting battles and weighting the
  density, and wrong for recognition: a capture can open on a transient grid
  before the client settles, and a Berlin Conquest-2 capture does exactly that.
- **A reading has to appear twice before the map switches**, for the same
  reason -- a single frame of a transient grid would otherwise swap the map
  mid-battle.

Where two layouts of one map genuinely report the same grid, as both Middle East
layouts do and as Berlin's transient reading does, nothing can tell them apart,
so the whole-map combination is used: it holds both, and guessing would be right
half the time.

`sample_map.py` now also saves `map.img`, the client's own picture of the map,
with every recording. It is the battle area exactly, needs no alias or published
size, and would identify a map on sight -- all of which the grid numbers only
approximate. Nothing reads it yet; it is being collected so that it can.

Mechanics worth knowing:

- The window is `WS_EX_LAYERED | WS_EX_TRANSPARENT`, so clicks fall through and
  the game never sees it.
- Hotkeys are polled with `GetAsyncKeyState` rather than registered with
  `RegisterHotKey`. Polling does not swallow the key, so whatever the game binds
  to Ctrl+Q still happens. Nothing is injected into the game.
- Tk makes exactly one colour transparent, so cells below an alpha floor are
  painted `#010203`, a colour the ramp never produces. Blending towards it
  instead would tint every faint cell near-black and fog the map.
- Tk only scales images by whole numbers, so resampling is done here and cached
  per size under `data/overlay-cache`: 1.1 s the first time at 700x700, instant
  after.
- It reads only the HTTP endpoint the game already serves to its own web map. No
  memory, no files, no input.
- **A battle on a map with nothing recorded clears the overlay rather than
  leaving the previous map up.** Showing the last map would be worse than
  showing nothing: it is a different place, drawn as if it were this one. The
  library is also re-read before concluding there is no heatmap, so a map built
  while the overlay is running is picked up without restarting it.

## Next steps, in order

1. **Volume.** Everything rests on one battle per map. Several battles on the
   same map and layout will show whether the binning and smoothing hold up.
   `heatmap/generate.py` already takes several `tracks.csv` files at once.
2. **Decode the type-4 position encoding**, checked against the answer key.
   That unblocks batch processing, both teams, and vehicle class from the
   replay rather than from map icons.
3. **Storage.** A battle is a few MB of CSV. Past a few dozen, move to Parquet
   or DuckDB as the brief suggests.
4. **The overlay** (Ctrl+Q large map, Ctrl+E minimap) — last, and only once the
   heatmaps are worth overlaying.
