"""Draw stitched tracks on the map as an SVG.

Takes `tracks.csv` from `tools/build_tracks.py` and the layout geometry from
`data/levels/<map>_<layout>.json` (capture points and spawns, read out of the
installed game by `bin/wtlevel.exe`) and writes one SVG. SVG keeps this
dependency-free and stays sharp at any zoom.

World X runs left to right and world Z runs bottom to top, so Z is flipped when
drawing because SVG y grows downward.

    python tools/plot_tracks.py data/live/<recording> --layout data/levels/berlin_dom.json
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

PX_PER_M = 0.55          # drawing scale
MARGIN_PX = 54
GRID_STEP_M = 250

STYLE = {
    "bg": "#12161a",
    "panel": "#1a2027",
    "grid": "#263039",
    "axis": "#54636f",
    "text": "#c9d4de",
    "muted": "#7e8d9a",
    "ally": "#4f9dff",
    "squad": "#5fd39a",
    "enemy": "#ff5a4d",
    "other": "#b9a24a",
    "player": "#ffd34d",
    "cap": "#5fd39a",
    "spawn_t1": "#4f9dff",
    "spawn_t2": "#ff5a4d",
}


# Missions name their areas per difficulty: Berlin has both "_hardcore"
# (Realistic) and "_arcade" variants, Tunisia only "_arcade". Prefer the
# Realistic one where it exists and fall back rather than finding nothing.
DIFFICULTY_SUFFIXES = ("_hardcore", "_arcade", "")


def pick_areas(layout: dict, needle: str) -> list[dict]:
    """Areas whose name contains `needle`, from the best available difficulty."""
    areas = [a for a in layout.get("captures", [])
             if needle in a.get("name", "") and not a.get("name", "").startswith("briefing_")]
    for suffix in DIFFICULTY_SUFFIXES:
        chosen = [a for a in areas if a["name"].endswith(suffix)]
        if chosen:
            return chosen
    return []


def battle_area_centre(layout: dict):
    """Centre of the playable square, which is also the map image's centre."""
    areas = pick_areas(layout, "battle_area")
    return (areas[0]["x"], areas[0]["z"]) if areas else None


def map_background(map_dir: Path, layout: dict):
    """Image bytes and world bounds for the map picture, or None.

    wt-tools publishes the image and the size of the area it covers but not
    where that area sits, so the centre comes from the mission file.
    """
    meta_path = map_dir / "map.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    size = meta.get("size_m")
    centre = battle_area_centre(layout)
    if not size or centre is None:
        return None
    image_path = map_dir / meta["image"]
    if not image_path.exists():
        return None
    cx, cz = centre
    half = size / 2
    return {
        "data": image_path.read_bytes(),
        "min_x": cx - half, "max_x": cx + half,
        "min_z": cz - half, "max_z": cz + half,
        "meta": meta,
    }


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class View:
    """Maps world metres onto SVG pixels."""

    def __init__(self, min_x, max_x, min_z, max_z):
        self.min_x, self.max_x = min_x, max_x
        self.min_z, self.max_z = min_z, max_z
        self.width = (max_x - min_x) * PX_PER_M + 2 * MARGIN_PX
        self.height = (max_z - min_z) * PX_PER_M + 2 * MARGIN_PX

    def px(self, x: float) -> float:
        return MARGIN_PX + (x - self.min_x) * PX_PER_M

    def py(self, z: float) -> float:
        # World Z grows upward; SVG y grows downward.
        return MARGIN_PX + (self.max_z - z) * PX_PER_M


def read_tracks(path: Path):
    tracks: dict[int, dict] = {}
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            tid = int(row["track_id"])
            track = tracks.setdefault(tid, {
                "team": row["team"], "icon": row["icon"],
                "is_player": row["is_player"] == "1", "points": [],
            })
            if row["is_player"] == "1":
                track["is_player"] = True
            track["points"].append((float(row["t"]), float(row["world_x"]), float(row["world_z"])))
    for track in tracks.values():
        track["points"].sort()
    return tracks


def track_length(points) -> float:
    return sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(points, points[1:]))


def grid_lines(view: View, out: list[str]) -> None:
    start_x = math.ceil(view.min_x / GRID_STEP_M) * GRID_STEP_M
    x = start_x
    while x <= view.max_x:
        px = view.px(x)
        out.append(f'<line x1="{px:.1f}" y1="{MARGIN_PX}" x2="{px:.1f}" '
                   f'y2="{view.height - MARGIN_PX:.1f}" stroke="{STYLE["grid"]}" stroke-width="1" stroke-opacity="0.35"/>')
        out.append(f'<text x="{px:.1f}" y="{view.height - MARGIN_PX + 18:.1f}" '
                   f'fill="{STYLE["muted"]}" font-size="11" text-anchor="middle">{int(x)}</text>')
        x += GRID_STEP_M

    start_z = math.ceil(view.min_z / GRID_STEP_M) * GRID_STEP_M
    z = start_z
    while z <= view.max_z:
        py = view.py(z)
        out.append(f'<line x1="{MARGIN_PX}" y1="{py:.1f}" x2="{view.width - MARGIN_PX:.1f}" '
                   f'y2="{py:.1f}" stroke="{STYLE["grid"]}" stroke-width="1" stroke-opacity="0.35"/>')
        out.append(f'<text x="{MARGIN_PX - 8:.1f}" y="{py + 4:.1f}" fill="{STYLE["muted"]}" '
                   f'font-size="11" text-anchor="end">{int(z)}</text>')
        z += GRID_STEP_M


def draw_layout(view: View, layout: dict, out: list[str]) -> None:
    for cap in pick_areas(layout, "capture_area"):
        name = cap["name"]
        cx, cz = view.px(cap["x"]), view.py(cap["z"])
        out.append(f'<circle cx="{cx:.1f}" cy="{cz:.1f}" r="22" fill="none" '
                   f'stroke="{STYLE["cap"]}" stroke-width="2" stroke-dasharray="5 4"/>')
        label = name.split("capture_area_")[-1].split("_")[0]
        out.append(f'<text x="{cx:.1f}" y="{cz + 5:.1f}" fill="{STYLE["cap"]}" font-size="14" '
                   f'font-weight="600" text-anchor="middle">{esc(label)}</text>')

    spawns = pick_areas(layout, "tank_spawn") or pick_areas(layout, "killarea")
    for cap in spawns:
        name = cap["name"]
        colour = STYLE["spawn_t1"] if "t1_" in name else STYLE["spawn_t2"]
        cx, cz = view.px(cap["x"]), view.py(cap["z"])
        out.append(f'<rect x="{cx - 7:.1f}" y="{cz - 7:.1f}" width="14" height="14" '
                   f'fill="none" stroke="{colour}" stroke-width="2"/>')
        side = "T1 spawn" if "t1_" in name else "T2 spawn"
        out.append(f'<text x="{cx:.1f}" y="{cz - 12:.1f}" fill="{colour}" font-size="11" '
                   f'text-anchor="middle">{side}</text>')


def draw_tracks(view: View, tracks: dict, out: list[str]) -> None:
    # Player last so it sits on top of everyone else.
    ordered = sorted(tracks.values(), key=lambda t: (t["is_player"], track_length(t["points"])))
    for track in ordered:
        points = track["points"]
        if len(points) < 2:
            continue
        path = " ".join(f"{view.px(x):.1f},{view.py(z):.1f}" for _, x, z in points)
        if track["is_player"]:
            colour, width, opacity = STYLE["player"], 3.0, 1.0
        else:
            colour = STYLE.get(track["team"], STYLE["other"])
            width, opacity = 1.6, 0.75
        out.append(f'<polyline points="{path}" fill="none" stroke="{colour}" '
                   f'stroke-width="{width}" stroke-opacity="{opacity}" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        # Mark where the route starts and ends.
        _, sx, sz = points[0]
        _, ex, ez = points[-1]
        out.append(f'<circle cx="{view.px(sx):.1f}" cy="{view.py(sz):.1f}" r="3" '
                   f'fill="{colour}" fill-opacity="{opacity}"/>')
        out.append(f'<rect x="{view.px(ex) - 2.5:.1f}" y="{view.py(ez) - 2.5:.1f}" '
                   f'width="5" height="5" fill="{colour}" fill-opacity="{opacity}"/>')


def legend(view: View, tracks: dict, meta: dict, out: list[str]) -> None:
    counts: dict[str, int] = defaultdict(int)
    for track in tracks.values():
        counts[track["team"]] += 1
    x = MARGIN_PX
    y = 26
    title = meta.get("title", "tracks")
    out.append(f'<text x="{x}" y="{y}" fill="{STYLE["text"]}" font-size="16" '
               f'font-weight="600">{esc(title)}</text>')
    subtitle = meta.get("subtitle", "")
    if subtitle:
        out.append(f'<text x="{x}" y="{y + 18}" fill="{STYLE["muted"]}" font-size="12">'
                   f'{esc(subtitle)}</text>')

    items = [
        (STYLE["player"], f"your vehicle"),
        (STYLE["ally"], f"your team ({counts.get('ally', 0)})"),
        (STYLE["squad"], f"your squad ({counts.get('squad', 0)})"),
        (STYLE["enemy"], f"enemy ({counts.get('enemy', 0)})"),
        (STYLE["cap"], "capture point"),
    ]
    lx = view.width - MARGIN_PX
    for i, (colour, label) in enumerate(items):
        ly = 26 + i * 17
        out.append(f'<line x1="{lx - 128:.1f}" y1="{ly - 4}" x2="{lx - 104:.1f}" y2="{ly - 4}" '
                   f'stroke="{colour}" stroke-width="3"/>')
        out.append(f'<text x="{lx - 98:.1f}" y="{ly}" fill="{STYLE["muted"]}" '
                   f'font-size="11">{esc(label)}</text>')

    out.append(f'<text x="{MARGIN_PX}" y="{view.height - 14:.1f}" fill="{STYLE["muted"]}" '
               f'font-size="11">world metres, {GRID_STEP_M} m grid  ·  '
               f'circle = route start, square = last seen</text>')


def render(tracks: dict, layout: dict, meta: dict, background=None) -> str:
    xs, zs = [], []
    for track in tracks.values():
        for _, x, z in track["points"]:
            xs.append(x)
            zs.append(z)
    for cap in pick_areas(layout, "capture_area") + pick_areas(layout, "tank_spawn"):
        xs.append(cap["x"])
        zs.append(cap["z"])
    if background is not None:
        xs += [background["min_x"], background["max_x"]]
        zs += [background["min_z"], background["max_z"]]
    if not xs:
        raise SystemExit("nothing to draw")

    pad = 120
    view = View(min(xs) - pad, max(xs) + pad, min(zs) - pad, max(zs) + pad)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{view.width:.0f}" '
           f'height="{view.height:.0f}" viewBox="0 0 {view.width:.0f} {view.height:.0f}">',
           f'<rect width="100%" height="100%" fill="{STYLE["bg"]}"/>',
           f'<rect x="{MARGIN_PX}" y="{MARGIN_PX}" '
           f'width="{view.width - 2 * MARGIN_PX:.1f}" height="{view.height - 2 * MARGIN_PX:.1f}" '
           f'fill="{STYLE["panel"]}" stroke="{STYLE["axis"]}" stroke-width="1"/>']
    if background is not None:
        href = ("data:image/png;base64,"
                + base64.b64encode(background["data"]).decode("ascii"))
        x0 = view.px(background["min_x"])
        y0 = view.py(background["max_z"])
        width = (background["max_x"] - background["min_x"]) * PX_PER_M
        height = (background["max_z"] - background["min_z"]) * PX_PER_M
        out.append(f'<image href="{href}" x="{x0:.1f}" y="{y0:.1f}" '
                   f'width="{width:.1f}" height="{height:.1f}" opacity="0.85"/>')
    grid_lines(view, out)
    draw_layout(view, layout, out)
    draw_tracks(view, tracks, out)
    legend(view, tracks, meta, out)
    out.append("</svg>")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--layout", type=Path, help="data/levels/<map>_<layout>.json")
    parser.add_argument("--map", type=Path, dest="map_dir",
                        help="data/maps/<map>/<mode> directory with the map image")
    parser.add_argument("--out", type=Path, help="where to write the SVG")
    args = parser.parse_args()

    tracks = read_tracks(args.recording / "tracks.csv")
    layout = json.loads(args.layout.read_text()) if args.layout and args.layout.exists() else {}

    samples = sum(len(t["points"]) for t in tracks.values())
    span = max((t["points"][-1][0] for t in tracks.values()), default=0)
    mission = layout.get("mission", "")
    meta = {
        "title": f"{Path(mission).stem or args.recording.name} — vehicle routes",
        "subtitle": f"{len(tracks)} tracks, {samples} samples over {span:.0f}s  ·  "
                    f"recorded from the client map during replay playback",
    }
    background = map_background(args.map_dir, layout) if args.map_dir else None
    if args.map_dir and background is None:
        print(f"no usable map image in {args.map_dir}; drawing without one")

    out_path = args.out or (args.recording / "tracks.svg")
    out_path.write_text(render(tracks, layout, meta, background), encoding="utf-8")
    print(f"{len(tracks)} tracks, {samples} samples -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
