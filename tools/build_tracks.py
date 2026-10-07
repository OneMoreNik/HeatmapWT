"""Stitch recorded map frames into one track per vehicle.

`collector/sample_map.py` records what the game draws on the map, frame by
frame. Those entries carry no identity: frame N's third tank is not necessarily
frame N+1's third tank, and the list reorders as units appear and disappear. So
tracks have to be rebuilt by following each icon from frame to frame.

Matching is nearest-neighbour within a distance a vehicle could plausibly cover
since the last sample, restricted to the same side and vehicle class, and
accepted only when both sides agree it is their closest partner. That mutual
check is what stops two tanks passing near each other from swapping tracks.

    python tools/build_tracks.py data/live/<recording>
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

# Vehicle classes drawn on the tank map. SPAA and tank destroyers count; the
# scenery, zones and airfield markers do not.
GROUND_ICONS = {
    "LightTank", "MediumTank", "HeavyTank", "TankDestroyer", "SPAA",
    "Airdefence", "Assault", "Player",
}

# The API types aircraft separately from ground vehicles, so a player who
# spawns a plane simply stops producing ground_model rows. Filtering on the
# type keeps those flight paths out of a ground heatmap without having to
# guess from the coordinates.
GROUND_TYPES = {"ground_model"}

# A fast light tank tops out near 80 km/h; allow headroom for a sparse sample
# rate without letting a match jump across the map.
MAX_SPEED_MS = 30.0
MIN_RADIUS_M = 25.0
# Drop a track that has not been seen for this long and start a new one.
TRACK_GAP_S = 12.0
# A track shorter than this is noise, not a route.
MIN_TRACK_SAMPLES = 4


def side_of(color: str) -> str:
    """Which side an icon belongs to, from its colour.

    Observed on a Berlin replay: amber ``#faC81E`` is the vehicle you were
    playing, green ``#67D756`` is your squad, and the blues (``#174DFF``,
    ``#1839A7``, ``#134AFF``) are the rest of your team. Enemies come out red.
    Shades vary, so the channels decide rather than an exact match.

    Amber has a high red channel, so it has to be tested before red, or the
    vehicle you were playing gets filed as an enemy.
    """
    if not color.startswith("#") or len(color) < 7:
        return "other"
    try:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
    except ValueError:
        return "other"
    if g > 150 and r > 150 and b < 110:
        return "player"
    if g > r + 40 and g > b + 40:
        return "squad"
    if b > r + 40 and b > g + 40:
        return "ally"
    if r > b + 40 and r > g + 40:
        return "enemy"
    return "other"


@dataclass
class Track:
    track_id: int
    team: str
    icon: str
    samples: list[tuple[float, float, float]] = field(default_factory=list)
    was_player: bool = False

    @property
    def last_t(self) -> float:
        return self.samples[-1][0]

    @property
    def last_xz(self) -> tuple[float, float]:
        return self.samples[-1][1], self.samples[-1][2]

    def length_m(self) -> float:
        total = 0.0
        for (_, x0, z0), (_, x1, z1) in zip(self.samples, self.samples[1:]):
            total += math.hypot(x1 - x0, z1 - z0)
        return total


class WorldMapping:
    """Map fractions to world metres, with y measured from the top edge."""

    def __init__(self, info: dict):
        self.min = info.get("map_min") or []
        self.max = info.get("map_max") or []
        self.usable = len(self.min) >= 2 and len(self.max) >= 2

    def to_world(self, x: float, y: float) -> tuple[float, float]:
        if not self.usable:
            return x, y
        return (self.min[0] + x * (self.max[0] - self.min[0]),
                self.max[1] - y * (self.max[1] - self.min[1]))


def read_frames(csv_path: Path, mapping: WorldMapping):
    """Yield (t, observations) per frame, in time order."""
    frames: dict[int, list[dict]] = {}
    times: dict[int, float] = {}
    with open(csv_path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["type"] not in GROUND_TYPES:
                continue
            if row["icon"] not in GROUND_ICONS:
                continue
            try:
                fx, fy = float(row["x"]), float(row["y"])
                t = float(row["t"])
                frame = int(row["frame"])
            except (TypeError, ValueError):
                continue
            # Markers outside the map image are off-field props, not vehicles.
            if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
                continue
            x, z = mapping.to_world(fx, fy)
            frames.setdefault(frame, []).append({
                "x": x, "z": z, "icon": row["icon"],
                "team": side_of(row["color"]), "kind": row["type"],
            })
            times[frame] = t
    for frame in sorted(frames):
        yield times[frame], frames[frame]


# Squad and team mates share a side and a vehicle can move between those
# groups, so their tracks may be joined. The spectated vehicle is deliberately
# not in this set: it has a colour of its own, so letting it match a team mate
# only ever lets the two steal each other's samples.
INTERCHANGEABLE = {"ally", "squad"}


def compatible(track: Track, obs: dict) -> bool:
    if track.team != obs["team"]:
        if not (track.team in INTERCHANGEABLE and obs["team"] in INTERCHANGEABLE):
            return False
    # The player's own vehicle is drawn with its own icon, so let that match
    # any class; otherwise a vehicle keeps its class for its whole life.
    if "Player" in (track.icon, obs["icon"]):
        return True
    return track.icon == obs["icon"]


def stitch(frames) -> list[Track]:
    active: list[Track] = []
    done: list[Track] = []
    next_id = 0

    for t, observations in frames:
        for track in [a for a in active if t - a.last_t > TRACK_GAP_S]:
            active.remove(track)
            done.append(track)

        # Candidate distances, restricted by how far a vehicle could have moved.
        pairs: list[tuple[float, int, int]] = []
        for oi, obs in enumerate(observations):
            for ti, track in enumerate(active):
                if not compatible(track, obs):
                    continue
                dt = max(t - track.last_t, 0.0)
                reach = max(MIN_RADIUS_M, MAX_SPEED_MS * dt)
                lx, lz = track.last_xz
                dist = math.hypot(obs["x"] - lx, obs["z"] - lz)
                if dist <= reach:
                    pairs.append((dist, oi, ti))
        pairs.sort()

        taken_obs: set[int] = set()
        taken_track: set[int] = set()
        for dist, oi, ti in pairs:
            if oi in taken_obs or ti in taken_track:
                continue
            taken_obs.add(oi)
            taken_track.add(ti)
            track, obs = active[ti], observations[oi]
            track.samples.append((t, obs["x"], obs["z"]))
            if obs["icon"] == "Player" or obs["team"] == "player":
                track.was_player = True

        for oi, obs in enumerate(observations):
            if oi in taken_obs:
                continue
            track = Track(track_id=next_id, team=obs["team"], icon=obs["icon"])
            next_id += 1
            track.samples.append((t, obs["x"], obs["z"]))
            track.was_player = obs["icon"] == "Player" or obs["team"] == "player"
            active.append(track)

    done.extend(active)
    return [t for t in done if len(t.samples) >= MIN_TRACK_SAMPLES]


def write_tracks(out_path: Path, tracks: list[Track]) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["track_id", "team", "icon", "is_player", "t", "world_x", "world_z"])
        for track in tracks:
            for t, x, z in track.samples:
                writer.writerow([track.track_id, track.team, track.icon,
                                 int(track.was_player), round(t, 2),
                                 round(x, 2), round(z, 2)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path, help="a data/live/<recording> directory")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="replay playback speed used while recording, so wall-clock "
                             "timestamps become battle seconds")
    args = parser.parse_args()

    info = json.loads((args.recording / "map_info.json").read_text())
    mapping = WorldMapping(info)
    tracks = stitch(read_frames(args.recording / "map_obj.csv", mapping))

    # Recording a replay played back at speed compresses wall-clock time;
    # rescale so timestamps, speeds and dwell times are in battle seconds.
    if args.speed != 1.0:
        for track in tracks:
            track.samples = [(t * args.speed, x, z) for t, x, z in track.samples]

    out_path = args.recording / "tracks.csv"
    write_tracks(out_path, tracks)

    by_team: dict[str, int] = {}
    for track in tracks:
        by_team[track.team] = by_team.get(track.team, 0) + 1
    print(f"{args.recording}")
    print(f"  map {mapping.min} .. {mapping.max}")
    print(f"  {len(tracks)} tracks: " + ", ".join(f"{n} {k}" for k, n in sorted(by_team.items())))
    print(f"  {sum(len(t.samples) for t in tracks)} samples -> {out_path}")
    longest = sorted(tracks, key=lambda t: t.length_m(), reverse=True)[:8]
    print("  longest routes:")
    for track in longest:
        tag = " (player)" if track.was_player else ""
        print(f"    #{track.track_id:<4} {track.team:<6} {track.icon:<14} "
              f"{len(track.samples):4d} samples  {track.length_m():7.0f} m"
              f"  {track.samples[0][0]:6.1f}..{track.samples[-1][0]:6.1f}s{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
