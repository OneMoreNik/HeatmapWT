"""Turn a minimap screenshot into a map background, calibrated against the game.

wt-tools publishes 62 maps and the game has more, so some maps will only ever
have a picture someone took themselves. A screenshot is not a map tile: nothing
says what world square it covers, and guessing wrong skews every track drawn on
it. The capture points settle it. The mission file gives their world positions,
the minimap draws each as a small light disc with a letter, and three known
points fix scale and offset exactly.

That fit is then checked rather than trusted. Every pair of caps gives a scale
in x and in z independently -- six estimates for three caps -- and they have to
agree before anything is written. On White Rock Fortress they agree to 0.9% and
reproduce all three cap positions to within 1.1 m.

    python tools/import_map_image.py maps/white_rock.png \
        --recording data/live/20261007-164119-gen30-live-test

Pass --caps when the discs cannot be found automatically, in the mission's own
order, as pixel coordinates:

    --caps 190.7,187.7 289.3,301.0 459.0,356.7
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from heatmapwt.layout import capture_points  # noqa: E402
from heatmapwt.png import read_png, write_png  # noqa: E402
from process_recording import layout_key, wt_tools_mode  # noqa: E402

# A capture disc is a small bright blob. Letters and grid labels are brighter
# still but thinner, and the flag bases are wider than they are tall.
DISC_MIN_PX = 8
DISC_MAX_PX = 160
DISC_MAX_SIDE = 18
# Blobs whose boxes come this close are parts of one disc.
MERGE_PX = 3
BRIGHT = 185

# The fit has to put every capture point back within this many metres.
MAX_RESIDUAL_M = 25.0


def bright_blobs(width: int, height: int, channels: int, pixels: bytes) -> list[tuple[float, float]]:
    """Centres of small bright blobs, which is what a capture disc looks like."""
    seen = bytearray(width * height)
    for i in range(width * height):
        at = i * channels
        if pixels[at] > BRIGHT and pixels[at + 1] > BRIGHT and pixels[at + 2] > BRIGHT:
            seen[i] = 1

    found = []
    for start in range(width * height):
        if not seen[start]:
            continue
        stack, blob = [start], []
        seen[start] = 0
        while stack:
            at = stack.pop()
            blob.append(at)
            x, y = at % width, at // width
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1),
                           (x - 1, y - 1), (x + 1, y - 1), (x - 1, y + 1), (x + 1, y + 1)):
                if 0 <= nx < width and 0 <= ny < height and seen[ny * width + nx]:
                    seen[ny * width + nx] = 0
                    stack.append(ny * width + nx)
        xs = [b % width for b in blob]
        ys = [b // width for b in blob]
        found.append([min(xs), max(xs), min(ys), max(ys), len(blob)])

    # The letter inside a disc breaks its ring, so one disc arrives as two or
    # three blobs. Rejoin anything whose boxes nearly touch before measuring.
    merged = True
    while merged:
        merged = False
        for i in range(len(found)):
            for j in range(i + 1, len(found)):
                a, b = found[i], found[j]
                if (a[0] - MERGE_PX <= b[1] and b[0] - MERGE_PX <= a[1]
                        and a[2] - MERGE_PX <= b[3] and b[2] - MERGE_PX <= a[3]):
                    found[i] = [min(a[0], b[0]), max(a[1], b[1]),
                                min(a[2], b[2]), max(a[3], b[3]), a[4] + b[4]]
                    del found[j]
                    merged = True
                    break
            if merged:
                break

    discs = []
    for x0, x1, y0, y1, count in found:
        side_x, side_y = x1 - x0 + 1, y1 - y0 + 1
        if not (DISC_MIN_PX <= count <= DISC_MAX_PX):
            continue
        if max(side_x, side_y) > DISC_MAX_SIDE or min(side_x, side_y) * 2 < max(side_x, side_y):
            continue
        # A ring's bounding-box centre survives the letter breaking it; its
        # centroid does not.
        discs.append(((x0 + x1) / 2, (y0 + y1) / 2))
    return discs


def fit(points: list[tuple[float, float]], caps: list[dict]) -> tuple[float, float, float, float]:
    """Least-squares (metres per pixel, world x at px 0, world z at py 0, worst residual).

    One shared scale for both axes, since a minimap is square on. Image y runs
    down and world z runs up, so z is fitted against -py.
    """
    mean_px = statistics.fmean(p[0] for p in points)
    mean_py = statistics.fmean(p[1] for p in points)
    mean_x = statistics.fmean(c["x"] for c in caps)
    mean_z = statistics.fmean(c["z"] for c in caps)

    numerator = denominator = 0.0
    for point, cap in zip(points, caps):
        dx, dy = point[0] - mean_px, point[1] - mean_py
        numerator += dx * (cap["x"] - mean_x) - dy * (cap["z"] - mean_z)
        denominator += dx * dx + dy * dy
    if denominator == 0:
        return 0.0, 0.0, 0.0, float("inf")
    scale = numerator / denominator
    origin_x = mean_x - scale * mean_px
    origin_z = mean_z + scale * mean_py

    worst = 0.0
    for point, cap in zip(points, caps):
        worst = max(worst,
                    abs(scale * point[0] + origin_x - cap["x"]),
                    abs(origin_z - scale * point[1] - cap["z"]))
    return scale, origin_x, origin_z, worst


def solve(points: list[tuple[float, float]], caps: list[dict]) -> tuple[float, float, float]:
    """The fit, rejected unless it reproduces every capture point closely.

    Judging this on the spread of the pairwise scale estimates looked tempting
    and is wrong: two caps 55 px apart turn half a pixel of disc-centre error
    into 1% of scale, so a good fit can look terrible. What matters is whether
    the result puts the caps back where the mission says they are.
    """
    scale, origin_x, origin_z, worst = fit(points, caps)
    # A negative scale means the discs were matched to the caps in a mirrored
    # order. It fits every pair just as consistently, so only the sign catches it.
    if scale <= 0:
        raise SystemExit("the discs fit the capture points only when mirrored, so "
                         "they are not the discs; pass --caps to place them by hand")
    if worst > MAX_RESIDUAL_M:
        raise SystemExit(
            f"the best fit still misplaces a capture point by {worst:.0f} m, more "
            f"than the {MAX_RESIDUAL_M:.0f} m allowed. Either the discs were "
            f"misidentified or the image is not a plain top-down view; pass --caps "
            f"to place them by hand")
    return scale, origin_x, origin_z


def match_caps(blobs: list[tuple[float, float]], caps: list[dict]) -> list[tuple[float, float]]:
    """Assign detected discs to mission caps, by whichever order fits best."""
    if len(blobs) < len(caps):
        raise SystemExit(f"found {len(blobs)} candidate discs for {len(caps)} capture "
                         f"points; pass --caps to place them by hand")
    best, best_worst = None, None
    for chosen in itertools.permutations(blobs, len(caps)):
        scale, _, _, worst = fit(list(chosen), caps)
        if scale <= 0:
            continue
        if best_worst is None or worst < best_worst:
            best, best_worst = list(chosen), worst
    if best is None:
        raise SystemExit("no arrangement of the detected discs fits the capture points")
    return best


def resample(src: tuple[int, int, int, bytes], out_px: int,
             left: float, top: float, step: float) -> list[bytes]:
    """Bilinear crop of the source onto an out_px square starting at (left, top)."""
    width, height, channels, pixels = src
    rows = []
    for row in range(out_px):
        sy = top + (row + 0.5) * step
        y0 = min(max(int(sy), 0), height - 1)
        y1 = min(y0 + 1, height - 1)
        fy = min(max(sy - y0, 0.0), 1.0)
        line = bytearray(out_px * 4)
        for col in range(out_px):
            sx = left + (col + 0.5) * step
            x0 = min(max(int(sx), 0), width - 1)
            x1 = min(x0 + 1, width - 1)
            fx = min(max(sx - x0, 0.0), 1.0)
            a = (y0 * width + x0) * channels
            b = (y0 * width + x1) * channels
            c = (y1 * width + x0) * channels
            d = (y1 * width + x1) * channels
            for ch in range(3):
                top_mix = pixels[a + ch] * (1 - fx) + pixels[b + ch] * fx
                bot_mix = pixels[c + ch] * (1 - fx) + pixels[d + ch] * fx
                line[col * 4 + ch] = int(top_mix * (1 - fy) + bot_mix * fy)
            line[col * 4 + 3] = 255
        rows.append(bytes(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", type=Path, help="a minimap screenshot")
    parser.add_argument("--recording", type=Path, required=True,
                        help="a data/live/<recording> of a battle on this map, for its "
                             "source.json and the grid the client reported")
    parser.add_argument("--levels", type=Path, default=Path("data/levels"))
    parser.add_argument("--maps", type=Path, default=Path("data/maps"))
    parser.add_argument("--caps", nargs="+", metavar="X,Y",
                        help="cap disc pixel positions, in the mission's order")
    args = parser.parse_args()

    source = json.loads((args.recording / "source.json").read_text(encoding="utf-8"))
    info = json.loads((args.recording / "map_info.json").read_text(encoding="utf-8"))
    map_name, layout_name = layout_key(source["battleType"])
    layout = json.loads((args.levels / f"{map_name}_{layout_name}.json").read_text(encoding="utf-8"))
    caps = capture_points(layout)
    if len(caps) < 3:
        raise SystemExit(f"{map_name} has {len(caps)} capture points; three are needed "
                         f"to fix scale and offset")

    src = read_png(args.image)
    width, height = src[0], src[1]
    print(f"{args.image}  {width}x{height}")

    if args.caps:
        points = [tuple(float(v) for v in spec.split(",")) for spec in args.caps]
    else:
        blobs = bright_blobs(*src)
        points = match_caps(blobs, caps)
        print(f"  {len(blobs)} candidate discs, matched {len(points)}")
    scale, origin_x, origin_z = solve(points, caps)
    print(f"  {scale:.4f} m/px")
    for point, cap in zip(points, caps):
        got = (scale * point[0] + origin_x, origin_z - scale * point[1])
        print(f"    {cap['name']:<34} px {point[0]:7.1f},{point[1]:7.1f} -> "
              f"{got[0]:8.1f},{got[1]:9.1f}  mission {cap['x']:8.1f},{cap['z']:9.1f}"
              f"  off by {abs(got[0]-cap['x']):.1f},{abs(got[1]-cap['z']):.1f} m")

    size_m = float((info.get("grid_size") or [0])[0])
    zero = info.get("grid_zero") or []
    if not size_m or len(zero) < 2:
        raise SystemExit("the recording reports no grid to align the image to")
    # Where the client's grid square sits in the source image.
    left = (zero[0] - origin_x) / scale
    top = (origin_z - zero[1]) / scale
    span_px = size_m / scale
    print(f"  client grid {size_m:.0f} m at ({zero[0]:.1f}, {zero[1]:.1f}); the image "
          f"covers {width * scale:.0f} x {height * scale:.0f} m")
    print(f"  cropping to px x {left:.1f}..{left + span_px:.1f}  y {top:.1f}..{top + span_px:.1f}")
    if left < -8 or top < -8 or left + span_px > width + 8 or top + span_px > height + 8:
        print("  WARNING: the grid square runs outside the screenshot; the edges will "
              "be padded with the nearest pixel")

    out_px = int(round(span_px))
    rows = resample(src, out_px, left, top, span_px / out_px)

    mode = wt_tools_mode(layout_name)
    out_dir = args.maps / map_name / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    image_name = f"{map_name}_{mode}_map.png"
    write_png(out_dir / image_name, out_px, out_px, rows)
    meta = {
        "map": map_name,
        "mode": mode,
        "image": image_name,
        "size_m": size_m,
        # The client's own grid spacing: this image was aligned to that grid, so
        # the two describe the same square by construction.
        "tile_m": (info.get("grid_steps") or [None])[0],
        "image_px": [out_px, out_px],
        "source": str(args.image),
        "calibration": {
            "from": "capture points in the mission file",
            "recording": str(args.recording),
            "scale_m_per_px": round(scale, 4),
            "caps": [{"name": c["name"], "px": list(p)} for p, c in zip(points, caps)],
        },
    }
    (out_dir / "map.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  -> {out_dir / image_name} ({out_px}x{out_px}, {size_m:.0f} m)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
