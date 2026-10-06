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

This is the one blocker for the first demo, and it is not where it first looked.

### The ECS stream is self-describing

`ecshashes.json` matters less than expected. Templates and component definitions
come **off the wire**: `ParseECSTemplate` reads the template id, its name, and
for each component both its name hash and its type hash. Template names decode
perfectly on current replays — `tank+player_unit` (61), `aircraft+player_unit`
(32), `sync_player` (20), `fortification` (197) — so the bitstream is being read
correctly. `ecshashes.json` only turns those hashes back into readable names.

What it does cost: `deserializeConstruction` aborts an entity if a component's
name hash is missing from the map, and `unit__className` lookups fail, so no
track can be tied to a vehicle or a player. Worth fixing, but it is not what
hides the positions.

The real finding: of 704 constructed entities, **every one carries zero
components**. Construction messages in this version ship no component values at
all; the values arrive afterwards as updates, and wrpl-inspector's ECS parser
only handles control bytes `0x24` and `0x25`, both construction. Updates are
dropped on the floor.

### The per-vehicle update stream

Grouping every packet by type, payload length and leading bytes leaves exactly
one candidate: **packet type 4, payload length 63**, 35,573 packets covering 43
entities at about 5.3 updates per entity per second over the whole battle. No
other shape comes close — the next largest covers 2 entities. The flight-model
parser's packets are type 2 and cover only a couple of units.

Those 63 bytes are:

| Offset | Size | Contents |
|---|---|---|
| 0–1 | 2 | `ff 0f` |
| 2–4 | 3 | varint entity id, stable for the life of a vehicle |
| 5–10 | 6 | constant marker `cc f0 35 00 fe 01` |
| 11–26 | 16 | unit quaternion — components square to 1.000 |
| 27–38 | 12 | three float32, magnitude roughly 8–9 |
| 39–50 | 12 | three float32, a unit vector |
| 51–62 | 12 | a 2-byte component index, a float32, then constant bytes |

### Why the vec3 at +27 is not the position

It is the only plausible candidate, and it fails a direct test against ground
truth. Berlin's two Ground RB spawns are 1016 m apart, almost entirely along Z,
and every vehicle sits on one of them at match start. Averaging each entity's
vector over the second before anyone moves splits the fleet **14 / 12 on the
third component** — the two teams, correctly — with a gap of 8.337 units,
implying 121.9 m per unit.

But the rest does not hold up. The first component spreads over 11.6 units
(1414 m at that scale) across vehicles that really start within about 200 m of
each other, one cluster is tight while the other is spread over 374 m, and the
largest step for a single vehicle is 9.38 units — 1143 m between two ticks
8 Hz apart. At match start the magnitude is near-identical across every vehicle
(≈8.93) and then drifts to ≈8.17; a position does not behave like that, a
direction does.

Scanning every byte offset in every packet for a float32 or float64 triple
inside Berlin's real world box (X 1024–3072, Z 0–2048) finds no consistent
signature either. So absolute positions are not present as plain floats
anywhere, and the compact stream most likely carries dead-reckoning state —
orientation, velocity, heading — with positions quantised or delta-encoded.

Decoding it means working out the update message format, which wrpl-inspector
never implemented. The old `PositionRetainerParser` read plain float64s at a
fixed offset, which the game no longer sends, so there is no prior art to lean
on here.

### Component ids are FNV-1a

Still worth recording, and still true: component ids are **FNV-1a 32-bit**
hashes of the component name. Checked against all 6647 known name/hash pairs in
`ecshashes.json` — 6647 exact matches, with FNV-1, djb2, sdbm, CRC32 and
MurmurHash3 each matching none. The types those components use are dominated by
a handful of primitives (float 1998, `ecs::string` 862, int 744, bool 659,
Point3 385, `ecs::Tag` 346, `ecs::Object` 331) that map onto BLK value types, so
the map can be regenerated from the installed game when it is needed.

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
