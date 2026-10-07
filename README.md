# HeatmapWT

A personal map-learning tool for War Thunder Ground Battles. It builds historical heatmaps of where heavy, medium, light and tank-destroyer vehicles move on each map and layout, and shows them as a semi-transparent overlay on the in-game map.

It uses only historical replay data. It does not read game memory, inject into the game, capture the screen or show live enemy positions.

Project brief (rendered): [docs/brief.html](docs/brief.html)

## Status

Working end to end for a single battle (Oct 2026). Replays are played back in
the client automatically and the positions the client draws on its own map are
recorded, then turned into per-class heatmaps. Three battles have gone through
the whole pipeline without anyone touching the game.

Start here: **[HANDOFF.md](HANDOFF.md)** — pipeline, setup on a new machine,
the calibration chain and how each link was verified, and the traps.

```bash
python collector/sync_replays.py                    # repo replays -> game folder
python collector/auto_replay.py --list              # read from the files, no game
python collector/auto_replay.py --rows 0,1,2        # play and record
python tools/process_recording.py data/live/*/      # tracks, plots, heatmaps, viewer
python tools/build_map.py --list                    # what you have, per map
python tools/build_map.py                           # one heatmap per map+layout
```

Two things are not solved. **Enemy vehicles never appear** — the client's map
API reports only your own team, so a battle yields one side. And positions in
**server replays**, which contain both teams and need no game running, are
still undecoded; that is what would let this scale past one battle at a time.
See [docs/replay-format.md](docs/replay-format.md).

## Layout

```text
heatmapwt/      replay header, zstd packet stream, vehicle roster
collector/      sync replays, drive the client, record, fetch map images
parser/wtcarve  server replay -> players, kills (no positions yet)
parser/wtresults any replay -> scoreboard, teams, who to follow
parser/wtlevel  map bounds, capture points and spawns from the installed game
parser/wtprobe  locate the movement packets in a new game version
tools/          stitch tracks, plot, process a recording, combine a map, import a map, build the viewer
heatmap/        dwell-weighted binning, smoothing, per-class output
vendor/patches/ changes wrpl-inspector needs to read current replays
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
