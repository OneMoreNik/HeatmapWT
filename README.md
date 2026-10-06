# HeatmapWT

A personal map-learning tool for War Thunder Ground Battles. It builds historical heatmaps of where heavy, medium, light and tank-destroyer vehicles move on each map and layout, and shows them as a semi-transparent overlay on the in-game map.

It uses only historical replay data. It does not read game memory, inject into the game, capture the screen or show live enemy positions.

Project brief (rendered): [docs/brief.html](docs/brief.html)

## Status

Proof of concept in progress (Oct 2026). Both Berlin server replays are archived
locally and parse: 32 players with teams, the vehicle roster, kill timestamps,
and the map's real world bounds and capture points read from the installed game.

One thing blocks the first plot: vehicle **positions**. The movement packets are
located (type 4, 8 Hz, 40 tracks) but their coordinates are bit-packed, and
wrpl-inspector's `ecshashes.json` is older than game version 101404, so it
decodes no components at all. Regenerating it is the next step and looks
tractable — component ids are FNV-1a 32-bit hashes of the component name, which
reproduces all 6647 known pairs exactly.

Format details and what is verified: [docs/replay-format.md](docs/replay-format.md).

## Layout

```text
heatmapwt/      Python: replay header, zstd packet stream, vehicle roster
collector/      download server replays by session id
parser/wtcarve  Go: server replay -> players.json, tracks.csv, kills.csv
parser/wtprobe  Go: locate the movement packets in a new game version
parser/wtlevel  Go: map bounds and capture points from the installed game
vendor/patches/ changes needed to make wrpl-inspector read current replays
```

Build the Go tools (needs Go and a clone of wrpl-inspector in `vendor/`):

```bash
cd parser/wtcarve && go build -o ../../bin/wtcarve.exe .
```

Then:

```bash
python tools/inspect_replay.py replays/*.wrpl
python collector/download_server_replay.py replays/*.wrpl
./bin/wtcarve.exe replays/server/<session hex>
```

## Findings

| Question | Answer | Source |
|---|---|---|
| Positions over time, all players | Yes. Server replays contain full-precision ground movement for every vehicle on both teams | [wrpl-inspector](https://github.com/maxsupermanhd/wrpl-inspector) (`wrpl/carve`) |
| Map, layout, battle type, difficulty | Yes, from the replay header | wrpl-inspector |
| Team, player, vehicle model | Yes | wrpl-inspector |
| Kill and death positions | Yes, for both killer and victim | wrpl-inspector |
| Vehicle class | Yes: `type_heavy_tank`, `type_medium_tank`, `type_light_tank`, `type_tank_destroyer`, `type_spaa` | [Datamine](https://github.com/gszabi99/War-Thunder-Datamine) `char.vromfs.bin_u/config/unittags.blkx` |
| BR | Yes: `economicRankHistorical / 3 + 1` | Datamine `wpcost.blkx` |
| Map bounds for alignment | `map_min` / `map_max` from `localhost:8111/map_info.json`, captured once per layout | [8111 API docs](https://github.com/lucasvmx/WarThunder-localhost-documentation) |
| Map images | `localhost:8111/map.img`, or per-layout images from [wt-tools.app](https://wt-tools.app/maps-overview.html) | |

Server replays download in 30-second parts from `https://wt-game-replays.warthunder.com/<sessionID>/0000.wrpl`, `0001.wrpl`, and so on. The session ID is in your local replay file.

The earlier WT-Heatmaps project collected live positions through [WT-Plotter](https://github.com/Sgambe33/WT-Plotter) and `localhost:8111`. Its site is down and its README marks it inactive. No public dataset was found.

## Rules

Gaijin staff have said overlays using the game's local data are fine, while enemy markers can count as ESP and reading rendered frames or memory is not allowed ([forum, May 2024](https://forum.warthunder.com/t/tools-using-data-provided-on-port-8111/106664)). This tool shows only precomputed historical data and uses manual calibration instead of screen capture.

## Planned architecture

```text
collector/   session IDs -> download server replay parts (raw .wrpl kept)
parser/      Go wrapper around wrpl-inspector -> per-battle Parquet
enrich/      datamine -> class, BR, nation per vehicle model
store/       Parquet + DuckDB
heatmap/     resample tracks -> bin -> Gaussian smoothing -> PNG per
             (map, layout, spawn side, class, BR band, layer)
viewer/      follow-a-player replay view and heatmap viewer
overlay/     Windows: transparent click-through window, Ctrl+Q / Ctrl+E, calibration
```

## Build order

1. Proof of concept: parse one server replay, plot all tracks on the map
   (blocked on position decoding; everything else in place)
2. Follow-a-player view for a single replay (route, stops, kills, timeline)
3. Batch pipeline: download, parse, enrich, store
4. Heatmaps: route, stop and engagement layers, with battle and sample counts
5. Windows overlay with calibration and hotkeys
6. Filters for BR, nation and vehicle; data confidence label

## Licence note

wrpl-inspector is AGPL-3.0. Fine for personal use; distributing a modified version requires publishing its source.
