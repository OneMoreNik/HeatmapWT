"""Turn stitched tracks into a heatmap image.

Counting raw samples would be wrong: a vehicle sitting still produces a sample
every tick and would outweigh a vehicle driving past, and the sample rate
varies with how fast the replay was played back. So each sample is weighted by
**how long it represents** — the time until the next sample on that track —
which makes the result a dwell-time density in vehicle-seconds per cell and
independent of the sample rate.

That same weighting is what separates routes from positions: a road shows up as
a long thin ridge of brief visits, a firing position as a small bright blob of
long ones. `--mode stops` keeps only samples where the vehicle barely moved, to
isolate the second kind.

The grid is smoothed with a Gaussian so a handful of battles does not read as a
scatter of dots, then written as an RGBA PNG sized to the map image, ready to
lay over it at whatever opacity the overlay wants.

    python heatmap/generate.py data/live/*/tracks.csv --map data/maps/berlin/conquest-2
    python heatmap/generate.py data/live/*/tracks.csv --map data/maps/berlin/conquest-2 --class heavy
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from heatmapwt.layout import battle_area_centre  # noqa: E402
from heatmapwt.png import write_png  # noqa: E402

# Icon names the game uses, grouped into the four classes the tool filters by.
CLASSES = {
    "heavy": {"HeavyTank"},
    "medium": {"MediumTank"},
    "light": {"LightTank"},
    "td": {"TankDestroyer"},
    # "Airdefence" is the static base AA, not a player SPAA; build_tracks
    # drops it before it reaches here.
    "spaa": {"SPAA"},
}
# "Player" is whichever vehicle was being spectated, so its class is unknown
# from the map data alone; it is kept only when no class filter is applied.
UNCLASSED = {"Player", "Assault"}

# Colour ramp from cool to hot. Alpha rises with intensity so empty ground stays
# clear and the map underneath shows through.
RAMP = [
    (0.00, (0, 0, 0, 0)),
    (0.10, (40, 70, 180, 60)),
    (0.30, (60, 180, 200, 120)),
    (0.55, (90, 210, 110, 170)),
    (0.78, (240, 200, 60, 205)),
    (1.00, (240, 70, 50, 235)),
]


def lerp(a, b, t):
    return a + (b - a) * t


def ramp_colour(value: float):
    """RGBA for a 0..1 intensity."""
    if value <= 0:
        return (0, 0, 0, 0)
    value = min(value, 1.0)
    for (lo, lo_c), (hi, hi_c) in zip(RAMP, RAMP[1:]):
        if value <= hi:
            t = 0.0 if hi == lo else (value - lo) / (hi - lo)
            return tuple(int(round(lerp(lo_c[i], hi_c[i], t))) for i in range(4))
    return RAMP[-1][1]


class Grid:
    """A dwell-time accumulator over a square of world space."""

    def __init__(self, min_x: float, min_z: float, size_m: float, cell_m: float):
        self.min_x, self.min_z = min_x, min_z
        self.size_m, self.cell_m = size_m, cell_m
        self.n = max(1, int(round(size_m / cell_m)))
        self.cells = [0.0] * (self.n * self.n)

    def add(self, x: float, z: float, weight: float) -> bool:
        col = int((x - self.min_x) / self.cell_m)
        # Row 0 is the top of the image, which is the high-Z edge of the world.
        row = int((self.min_z + self.size_m - z) / self.cell_m)
        if not (0 <= col < self.n and 0 <= row < self.n):
            return False
        self.cells[row * self.n + col] += weight
        return True

    def blur(self, sigma_cells: float) -> None:
        """Separable Gaussian, so a few battles read as density not dots."""
        if sigma_cells <= 0:
            return
        radius = max(1, int(math.ceil(sigma_cells * 3)))
        kernel = [math.exp(-(d * d) / (2 * sigma_cells * sigma_cells))
                  for d in range(-radius, radius + 1)]
        total = sum(kernel)
        kernel = [k / total for k in kernel]
        n = self.n

        pass1 = [0.0] * (n * n)
        for row in range(n):
            base = row * n
            for col in range(n):
                acc = 0.0
                for k, weight in enumerate(kernel):
                    src = col + k - radius
                    if 0 <= src < n:
                        acc += self.cells[base + src] * weight
                pass1[base + col] = acc

        for col in range(n):
            for row in range(n):
                acc = 0.0
                for k, weight in enumerate(kernel):
                    src = row + k - radius
                    if 0 <= src < n:
                        acc += pass1[src * n + col] * weight
                self.cells[row * n + col] = acc

    def percentile(self, q: float) -> float:
        """Used to scale the ramp, so one hot cell cannot flatten everything."""
        values = sorted(v for v in self.cells if v > 0)
        if not values:
            return 0.0
        return values[min(len(values) - 1, int(len(values) * q))]


def read_tracks(paths: list[Path], wanted: set[str] | None):
    """Yield (icon, [(t, x, z), ...]) per track across every input file."""
    for path in paths:
        tracks: dict[tuple[str, int], list] = {}
        icons: dict[tuple[str, int], str] = {}
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = (str(path), int(row["track_id"]))
                icons[key] = row["icon"]
                tracks.setdefault(key, []).append(
                    (float(row["t"]), float(row["world_x"]), float(row["world_z"])))
        for key, samples in tracks.items():
            icon = icons[key]
            if wanted is not None and icon not in wanted:
                continue
            if wanted is None and icon in UNCLASSED:
                pass  # keep: unfiltered view shows everything
            samples.sort()
            yield icon, samples


def accumulate(grid: Grid, tracks, mode: str, stop_speed: float, max_gap: float):
    """Add dwell-weighted samples, returning counts for the confidence label."""
    stats = {"tracks": 0, "samples": 0, "placed": 0, "seconds": 0.0}
    for _, samples in tracks:
        stats["tracks"] += 1
        for (t0, x0, z0), (t1, x1, z1) in zip(samples, samples[1:]):
            dt = t1 - t0
            # A long gap means the vehicle was out of sight, not parked there.
            if dt <= 0 or dt > max_gap:
                continue
            speed = math.hypot(x1 - x0, z1 - z0) / dt
            if mode == "stops" and speed > stop_speed:
                continue
            if mode == "routes" and speed <= stop_speed:
                continue
            stats["samples"] += 1
            stats["seconds"] += dt
            if grid.add(x0, z0, dt):
                stats["placed"] += 1
    return stats


def render(grid: Grid, out_px: int, clip: float) -> list[bytes]:
    peak = grid.percentile(clip) or max(grid.cells) or 1.0
    rows = []
    for py in range(out_px):
        row = bytearray()
        gy = min(grid.n - 1, py * grid.n // out_px)
        for px in range(out_px):
            gx = min(grid.n - 1, px * grid.n // out_px)
            value = grid.cells[gy * grid.n + gx] / peak
            row += bytes(ramp_colour(value))
        rows.append(bytes(row))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tracks", nargs="+", type=Path, help="tracks.csv files")
    parser.add_argument("--map", dest="map_dir", type=Path,
                        help="data/maps/<map>/<mode>, for the area size. Optional: "
                             "wt-tools does not publish every map, and --size "
                             "covers the rest")
    parser.add_argument("--size", type=float,
                        help="side of the playable square in metres. Overrides the "
                             "size published with the map image: the client's own "
                             "grid_size is authoritative, and wt-tools is sometimes "
                             "wrong (it gives Test-Site 2271 as 1600 m against the "
                             "client's 1700 m)")
    parser.add_argument("--layout", type=Path, required=True,
                        help="data/levels/<map>_<layout>.json, for the area centre")
    parser.add_argument("--class", dest="vclass", choices=sorted(CLASSES),
                        help="limit to one vehicle class")
    parser.add_argument("--mode", choices=["all", "routes", "stops"], default="all",
                        help="all movement, only moving, or only near-stationary")
    parser.add_argument("--cell", type=float, default=10.0, help="grid cell size in metres")
    parser.add_argument("--sigma", type=float, default=22.0, help="smoothing radius in metres")
    parser.add_argument("--stop-speed", type=float, default=1.5,
                        help="m/s below which a vehicle counts as stopped")
    parser.add_argument("--max-gap", type=float, default=10.0,
                        help="ignore gaps longer than this many seconds")
    parser.add_argument("--clip", type=float, default=0.99,
                        help="percentile mapped to the top of the colour ramp")
    parser.add_argument("--px", type=int, default=1024, help="output image size")
    parser.add_argument("--out", type=Path, help="output PNG path")
    parser.add_argument("--centre", metavar="X,Z",
                        help="centre of the square to draw, in world metres. The "
                             "client reports this in every recording and it is exact; "
                             "without it the mission's battle area is guessed at")
    args = parser.parse_args()

    map_meta = {}
    if args.map_dir and (args.map_dir / "map.json").exists():
        map_meta = json.loads((args.map_dir / "map.json").read_text())
    layout = json.loads(args.layout.read_text())
    given = tuple(float(v) for v in args.centre.split(",")) if args.centre else None
    centre = given or battle_area_centre(layout)
    size_m = args.size or map_meta.get("size_m")
    if centre is None or not size_m:
        raise SystemExit("need an area centre (from --layout) and a size "
                         "(from --map or --size)")
    min_x, min_z = centre[0] - size_m / 2, centre[1] - size_m / 2

    wanted = CLASSES[args.vclass] if args.vclass else None
    grid = Grid(min_x, min_z, size_m, args.cell)
    stats = accumulate(grid, read_tracks(args.tracks, wanted),
                       args.mode, args.stop_speed, args.max_gap)
    grid.blur(args.sigma / args.cell)

    name = f"heatmap-{args.vclass or 'all'}-{args.mode}.png"
    out_path = args.out or (args.map_dir / name)
    write_png(out_path, args.px, args.px, render(grid, args.px, args.clip))

    sidecar = {
        "map": map_meta.get("map"), "mode": map_meta.get("mode"),
        "vehicle_class": args.vclass or "all", "weighting": args.mode,
        "world": {"min_x": min_x, "min_z": min_z, "size_m": size_m},
        "cell_m": args.cell, "sigma_m": args.sigma,
        "sources": [str(p) for p in args.tracks],
        "counts": {
            "battles": len(args.tracks), "vehicles": stats["tracks"],
            "samples": stats["samples"], "vehicle_seconds": round(stats["seconds"], 1),
            # Samples that landed in the square. Short of "samples" means
            # part of the data falls outside what is being drawn, which is
            # how combining two layouts of one map shows up.
            "placed": stats["placed"],
        },
    }
    out_path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2))

    off = stats["samples"] - stats["placed"]
    print(f"{out_path}")
    print(f"  class {args.vclass or 'all'}, weighting {args.mode}")
    print(f"  {stats['tracks']} vehicles, {stats['samples']} steps, "
          f"{stats['seconds']:.0f} vehicle-seconds"
          + (f", {off} outside the map square" if off else ""))
    print(f"  {grid.n}x{grid.n} cells of {args.cell:g} m, smoothed {args.sigma:g} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
