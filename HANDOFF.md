# Handoff

Read this first in a new session, or on a different machine. It says what
works, what does not, and what to do next. Format details live in
[docs/replay-format.md](docs/replay-format.md); the original goal is in
[docs/brief.html](docs/brief.html).

## The one-paragraph version

The goal is historical heatmaps of where each vehicle class moves on each War
Thunder ground map, shown as an overlay. Two ways to get vehicle positions were
tried. **Recording the client's own map during replay playback works** and is
what produced everything so far, but it needs a supervised playback per battle
and only ever shows your own team. **Parsing server replays offline** is the
one that can scale to thousands of battles and both teams, and it is blocked on
one specific thing: the position encoding in the packet stream.

## What works today

| Step | Tool | State |
|---|---|---|
| Read a replay header | `heatmapwt/wrpl.py` | Map, layout, session id, start time |
| Download server replays | `collector/download_server_replay.py` | Both Berlin sessions archived |
| Parse a server replay | `bin/wtcarve.exe` | 32 players with names and teams, kills, roster. **No positions** |
| Map bounds and capture points | `bin/wtlevel.exe` | Read from the installed game, offline |
| Map images | `collector/fetch_maps.py` | 62 maps indexed from wt-tools.app |
| Record positions during playback | `collector/sample_map.py` | ~20 Hz, matches the game's own refresh rate |
| Rebuild per-vehicle tracks | `tools/build_tracks.py` | Validated against known spawns |
| Plot tracks on the map | `tools/plot_tracks.py` | SVG, map image underneath |
| Generate heatmaps | `heatmap/generate.py` | Dwell-weighted, smoothed, per class |
| Viewer | `tools/build_viewer.py` | Self-contained HTML, class switcher, opacity |

Everything is pure Python standard library plus Go for the replay parser. No
pip installs. Python 3.14 is required — `compression.zstd` is used to read
replay packet streams.

## Setup on a new machine

1. **Go** (for the replay parser): `winget install GoLang.Go`
2. **wrpl-inspector**, which is not vendored because it is AGPL:
   ```
   git clone https://github.com/maxsupermanhd/wrpl-inspector vendor/wrpl-inspector
   cd vendor/wrpl-inspector && git apply ../patches/*.patch
   ```
   The patches are required. Without them it cannot read current replays at all.
3. Build: `cd parser/wtcarve && go build -o ../../bin/wtcarve.exe .` and the
   same for `parser/wtlevel` and `parser/wtprobe`.
4. Point `bin/wtlevel.exe -game` at the War Thunder install if it is not
   `D:/Games/WarThunder`.

## Calibration, and how it was verified

Everything hangs off one chain, checked rather than assumed:

- The API's `x`/`y` are fractions of the map image. **The y fraction runs
  top-down while world Z runs bottom-up**, so Z is measured from `map_max`.
- Berlin's capture points then come out at Z 1119, 1140 and 1115 against the
  mission file's 1118.7, 1140.5 and 1115.2.
- Every stitched track starts within 5–86 m of a real spawn point.
- The map image covers a square whose **size** comes from wt-tools and whose
  **centre** is the mission's battle area. For Berlin Domination that predicts
  pixel positions for three capture points and both spawns to within a few
  pixels of the markers drawn on the published image.

If a future change breaks alignment, re-check in that order.

## Known traps

- **Connect to `127.0.0.1`, never `localhost`.** On Windows `localhost`
  resolves to `::1` first, the game listens only on IPv4, and every request
  pays the full connect timeout. This capped sampling at 0.5 Hz and was
  misdiagnosed as the game throttling in the background. It is not: window
  focus makes no difference.
- **The session id is at header offset `0x2DC`**, not `0x2E8`. The wrong offset
  yields an id-shaped number that silently 404s.
- **Replay packet streams are zstd**, not zlib. Game content BLKs are
  `SLIM_ZSTD_DICT` and need the archive's name map and dictionary.
- **The spectated vehicle must not be matched against team mates** when
  stitching. It has its own colour; letting them match lets the two steal each
  other's samples.
- **Record with `--speed`** matching the replay playback speed, or every
  timestamp, speed and dwell time is wrong by that factor.

## The blocker: positions in server replays

Worth reading [docs/replay-format.md](docs/replay-format.md) in full before
attacking this. The short version:

- The per-vehicle update stream is found: **packet type 4, payload length 63**,
  marker `cc f0 35 00 fe 01` at `payload[5:11]`, varint entity id at
  `payload[2:]`, ~5.3 updates per vehicle per second. It is the only
  per-vehicle stream in the file.
- Its layout is mapped: unit quaternion at +11, a float32 triple at +27,
  a unit vector at +39, then component-indexed values.
- The triple at +27 is **not** the position. It splits the fleet 14/12 by team
  correctly, but vehicles that start within 200 m of each other spread over
  1400 m, single steps reach 1143 m, and its magnitude is near-identical across
  every vehicle at spawn. That is a direction, not a place.
- Scanning every byte offset for a float triple inside Berlin's real world box
  finds nothing consistent, so positions are quantised or delta-encoded.

**What makes this tractable now that was not before:** there is a verified
answer key. `data/live/20261007-094704-gen1-conq2-phantom/tracks.csv` holds
Ph4nt0m_Bl4de's four lives from session `127b00490002868f`, on a map aligned to
within metres. Any candidate decoding can be checked directly against it
instead of guessed at.

Also unresolved, and probably the same root cause: `ecshashes.json` predates
this game version, so construction messages decode zero components and no track
can be tied to a vehicle model or player. Component ids are **FNV-1a 32-bit**
of the component name — verified against all 6647 known pairs, with no other
candidate hash matching one. The types are needed too and would come from the
game's template BLKs, which are now readable.

## Recording a battle, end to end

```bash
# 1. start the recorder, then play the replay in the client
python collector/sample_map.py --hz 20 --label berlin-conq2

# 2. rebuild tracks (--speed must match the playback speed used)
python tools/build_tracks.py data/live/<recording> --speed 16

# 3. look at the routes
python tools/plot_tracks.py data/live/<recording> \
    --layout data/levels/berlin_conq2.json --map data/maps/berlin/conquest-2

# 4. heatmaps, one per class
python heatmap/generate.py data/live/*/tracks.csv \
    --map data/maps/berlin/conquest-2 --layout data/levels/berlin_conq2.json \
    --class heavy --out data/heatmaps/conq2-heavy.png

# 5. viewer
python tools/build_viewer.py data/maps/berlin/conquest-2 \
    --heatmaps data/heatmaps --prefix conq2 --out data/heatmaps/berlin-conq2.html
```

Supporting data, fetched once per map:

```bash
python collector/fetch_maps.py --list
python collector/fetch_maps.py berlin --mode conquest-2
./bin/wtlevel.exe -level levels/avg_berlin.bin \
    -mission gamedata/missions/cta/tanks/berlin/berlin_conq2.blk \
    -out data/levels/berlin_conq2.json
```

## What the playback method cannot do

Established from three recordings and roughly 100,000 rows, not inferred:

- **Enemy vehicles never appear.** Every `ground_model` the API reports is on
  your own team. Red appears only for enemy spawn zones, airfields and
  contested capture zones. The client draws enemy markers on screen but does
  not expose them over HTTP, almost certainly deliberately.
- **One supervised playback per battle.** Even at 15x that is a minute of
  attention each, and the client has to be running.

So this path is right for validating geometry and for studying a particular
player, and wrong as the pipeline for thousands of battles.

## Next steps, in order

1. **Heatmap quality on real volume.** Everything so far rests on one battle.
   Several battles on the same map and layout will show whether the binning and
   smoothing settings hold up.
2. **Decode the type-4 position encoding**, checked against the answer key
   above. This unblocks batch processing, both teams, and vehicle classes from
   the replay rather than from map icons.
3. **Pick the player to follow automatically.** The results BLK at the end of
   every replay carries each player's name, score and kills, so the
   highest-scoring player can be chosen without watching the battle. Only the
   positions are missing.
4. **Storage.** One battle is a few MB of CSV. Past a few dozen, move to
   Parquet or DuckDB as the brief suggests.
5. **The overlay** (Ctrl+Q large map, Ctrl+E minimap) — last, and only once the
   heatmaps are worth overlaying.
