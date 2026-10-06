# Replay format notes

What has been verified against real replays, so none of it has to be rediscovered.
All of it comes from two Berlin Ground RB battles recorded on 2026-10-06 with
game version **101404**, cross-checked against
[wrpl-inspector](https://github.com/maxsupermanhd/wrpl-inspector) (branch `v3`,
newest commit 2026-08-22).

## Header

Fixed size **1234 bytes** (`0x4D2`), then the `mission_settings` BLK, then the
packet stream, then the results BLK. Offsets confirmed on both replays:

| Offset | Size | Field | Example |
|---|---|---|---|
| `0x000` | 4 | magic | bytes `e5 ac 00 10` |
| `0x004` | 4 | version | `101404` |
| `0x008` | 128 | level | `levels/avg_berlin.bin` |
| `0x088` | 260 | level settings (mission file) | `gamedata/missions/cta/tanks/berlin/berlin_dom.blk` |
| `0x18C` | 128 | battle type | `berlin_Dom` |
| `0x20C` | 128 | environment | `day` |
| `0x28C` | 32 | visibility | `rain` |
| `0x2AC` | 4 | results BLK offset | |
| `0x2DC` | 8 | **session id** | `127b0049000219ad` |
| `0x2E4` | 1 | replay part number | `0` for a client replay |
| `0x2E6` | 1 | `0x5a` in a server replay, `0` in a client one | |
| `0x2EC` | 2 | mission settings BLK size | `1160` |
| `0x30C` | 128 | localisation name | `missions/_Dom;berlin/name` |
| `0x38C` | 4 | battle start, unix seconds | |

The packet stream begins at `1234 + settingsBLKSize`.

Getting the session id wrong is easy and silent: an earlier guess of `0x2E8`
produced an id-shaped number that simply 404s. `0x2DC` is the one that works.

## Compression

The packet stream is a run of **concatenated zstd frames**. wrpl-inspector
expects zlib there, which was correct for older replays, so it fails outright on
current ones; `vendor/patches/` carries the patch that picks the codec from the
frame magic. Python's stdlib `compression.zstd` reads the stream directly, which
is what `heatmapwt/wrpl.py` does.

## Server replays

```
https://wt-game-replays.warthunder.com/<16 hex digits>/0000.wrpl
```

302s to `wt-replays-cdnnow.cdn.gaijin.net`, so redirects must be followed. Parts
run `0000`, `0001`, ...; small even parts carry mission settings, large odd parts
carry the packets, and the first and last odd parts carry the results BLK. The
two Berlin sessions came to 14 parts (15.7 MB) and 26 parts (26.1 MB).

A 404 on part 0 means the archive does not have that session, and server replays
are not kept forever — download soon after playing.

## What parses today

From a server replay, via `bin/wtcarve.exe`:

- 32 players with names, clan tags and teams
- 704 ECS entities
- 57 kill events with timestamps
- the full vehicle roster (also readable from a client replay, see `heatmapwt/roster.py`)

## What does not parse yet: positions

This is the one blocker for the first demo.

Ground movement updates **are** found: packet type 4, a constant marker
`cc f0 35 00 fe 01` at `payload[5:11]`, and a varint entity id at `payload[2:]`.
Grouping by that id gives 40 tracks at about 8 updates per second over the whole
battle, which is the right shape for a 32-player Ground RB match.

The payload is a 63-byte component stream, not a struct: a unit quaternion at
`+11` (components square to 1.000), a 3-vector at `+27`, another unit vector at
`+39`, then 2-byte component ids with values after them. Searching every byte
offset for a float32 or float64 triple inside Berlin's real world box
(X 1024–3072, Z 0–2048) finds no consistent signature, so absolute positions are
**bit-packed or quantised**, not stored as plain floats at a fixed offset.

Decoding them needs wrpl-inspector's ECS component parser, and that is where the
real fault is: `vendor/wrpl-inspector/data/ecshashes.json` predates game version
101404, so of the 704 entities it builds, **zero have any decoded component**.
That one staleness explains every remaining gap — no vehicle models, no team per
track, no kill positions, no movement fields.

### Why that is fixable

Component ids are **FNV-1a 32-bit** hashes of the component name. Checked
against all 6647 known name/hash pairs already in `ecshashes.json`: 6647 exact
matches, and no other candidate hash (FNV-1, djb2, sdbm, CRC32, MurmurHash3)
matched a single one.

So the map can be regenerated from the installed game. The remaining piece is
each component's *type*, which the wire format does not carry — it has to come
from the game's own template BLKs. Those are now readable: see below.

## Reading installed game content

`bin/wtlevel.exe` reads the client's own archives, so map alignment needs nothing
from a running game and nothing downloaded.

BLKs inside `aces.vromfs.bin` and `mis.vromfs.bin` are `SLIM_ZSTD_DICT`
(format byte `0x05`), which wrpl-inspector rejected. They need two things that
each archive already contains: the name map under the `0xff`-prefixed `?nm`
entry, and a zstd dictionary stored as a single `<sha256>.dict` entry at the
archive root. `vendor/patches/` carries that support.

### Berlin, from `levels/avg_berlin.blk`

`tankMapCoord0 = [1024, 0]`, `tankMapCoord1 = [3072, 2048]` — the tank minimap
covers a 2048 x 2048 m square of world space. This replaces capturing
`map_info.json` from `localhost:8111`.

### Berlin layouts, from `mis.vromfs.bin`

Realistic Battles use the `_hardcore` variants. Written to `data/levels/`.

| Layout | Capture points (x, z) | Team 1 spawn | Team 2 spawn |
|---|---|---|---|
| `berlin_dom` | (2112.9, 1118.7), (2418.6, 1140.5), (2633.1, 1115.2) | (2389.1, 1604.6) | (2383.1, 588.6) |
| `berlin_conq2` | (2418.6, 1140.5) | (2394.2, 1609.0) | (2383.2, 593.1) |

Both battles are on the same map but different layouts, which is why they are
stored separately: the capture points differ, so the movement will too.
