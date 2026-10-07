"""Turn a raw recording into tracks, plots and heatmaps in one step.

`collector/auto_replay.py` writes a `source.json` beside each recording naming
the replay it came from, so everything else can be worked out from there: the
map and layout from the replay header, the layout geometry from the installed
game, and the map image from wt-tools. Missing pieces are fetched rather than
assumed, so this runs on a machine that has never seen the map before.

    python tools/process_recording.py data/live/<recording>
    python tools/process_recording.py data/live/*/ --skip-existing
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_tracks import folder_start  # noqa: E402
from fetch_maps import resolve_map_key  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CLASSES = ["all", "heavy", "medium", "light", "td", "spaa"]
WEIGHTINGS = ["routes", "stops"]

# wt-tools names a mode "domination-1", the replay calls it "berlin_Dom".
MODE_NAMES = {
    "dom": "domination", "conq": "conquest", "bttl": "battle", "battle": "battle",
}


def run(args: list[str], quiet: bool = True, what: str = "") -> subprocess.CompletedProcess:
    """Run a step, and say so when it fails.

    Capturing output quietly once hid every heatmap failing on a map whose
    mission names its areas differently: the pipeline reported success and
    produced nothing.
    """
    result = subprocess.run([str(a) for a in args], capture_output=quiet, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        print(f"  FAILED: {what or args[1]}"
              + (f" -- {detail[-1][:160]}" if detail else ""))
    return result


def layout_key(battle_type: str) -> tuple[str, str]:
    """"berlin_Conq2" -> ("berlin", "conq2"); the map and its layout.

    Split on the LAST underscore: a map name may contain its own, as in
    "soviet_range_Dom" or "eastern_europe_02_Dom".
    """
    map_name, _, layout = battle_type.rpartition("_")
    if not map_name:
        map_name, layout = layout, ""
    return map_name.lower(), layout.lower()


def wt_tools_mode(layout: str) -> str:
    """"conq2" -> "conquest-2"; "dom" -> "domination-1"."""
    digits = "".join(c for c in layout if c.isdigit())
    word = "".join(c for c in layout if not c.isdigit())
    return f"{MODE_NAMES.get(word, word)}-{digits or '1'}"


def ensure_layout(source: dict, out: Path) -> Path | None:
    """Capture points and spawns, extracted from the installed game if absent."""
    map_name, layout = layout_key(source["battleType"])
    path = out / f"{map_name}_{layout}.json"
    if path.exists():
        return path
    wtlevel = Path("bin/wtlevel.exe")
    if not wtlevel.exists():
        print(f"  {path} missing and {wtlevel} not built")
        return None
    out.mkdir(parents=True, exist_ok=True)
    result = run([wtlevel, "-level", source["level"], "-mission", source["mission"],
                  "-out", path])
    if not path.exists():
        print(f"  could not extract the layout: {result.stderr.strip()[:200]}")
        return None
    print(f"  extracted layout -> {path}")
    return path


def ensure_map(source: dict, out: Path) -> Path | None:
    """Map image for this layout, downloaded from wt-tools if it publishes one.

    Returns None when it does not, which is normal: wt-tools has 62 maps and
    the game has more. The caller falls back to the client's own grid.
    """
    map_name, layout = layout_key(source["battleType"])
    mode = wt_tools_mode(layout)

    manifest_path = out / "manifest.json"
    key = map_name
    if manifest_path.exists():
        key = resolve_map_key(json.loads(manifest_path.read_text(encoding="utf-8")),
                              map_name) or map_name
    path = out / key / mode
    if (path / "map.json").exists():
        return path

    print(f"  fetching the map image for {map_name}"
          + (f" (published as {key})" if key != map_name else "") + f"/{mode}")
    run([sys.executable, "collector/fetch_maps.py", map_name, "--mode", mode,
         "--out", out], quiet=True)
    return path if (path / "map.json").exists() else None


def last_timestamp(csv_path: Path) -> float | None:
    """The `t` of the last row, read from the end of the file.

    A recording's map_obj.csv runs to hundreds of megabytes, so this seeks
    rather than reads.
    """
    try:
        with open(csv_path, "rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 8192))
            tail = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        head = line.split(",", 1)[0]
        try:
            return float(head)
        except ValueError:
            continue
    return None


def same_grid(a: dict, b: dict) -> bool:
    for key in ("grid_size", "grid_zero", "map_min", "map_max"):
        pa, pb = a.get(key) or [], b.get(key) or []
        if len(pa) != len(pb) or any(abs(x - y) > 1.0 for x, y in zip(pa, pb)):
            return False
    return True


def battle_group(recording: Path) -> list[Path]:
    """`recording` plus any folders that continue the same battle.

    `sample_map` opens a new folder whenever `map_generation` changes, and that
    can tick mid-battle: Test-Site 2271 came out as 8.8 minutes in one folder
    and 13.9 in the next, and processing only the first silently threw away two
    thirds of the battle. A follower counts as a continuation when it describes
    the same grid and starts where the previous one stopped.
    """
    siblings = sorted(d for d in recording.parent.iterdir()
                      if d.is_dir() and d.name > recording.name
                      and (d / "map_info.json").exists())
    group = [recording]
    for candidate in siblings:
        previous = group[-1]
        start_prev = folder_start(previous)
        start_next = folder_start(candidate)
        duration = last_timestamp(previous / "map_obj.csv")
        if start_prev is None or start_next is None or duration is None:
            break
        if abs((start_next - start_prev) - duration) > 60.0:
            break
        if not same_grid(json.loads((previous / "map_info.json").read_text(encoding="utf-8")),
                         json.loads((candidate / "map_info.json").read_text(encoding="utf-8"))):
            break
        group.append(candidate)
    return group


def client_grid_size(info: dict, map_meta: dict | None) -> float | None:
    """The playable square the client reported, or None to use the published one.

    The client's grid is usually the battle area exactly, and is sometimes more
    accurate than the size published with the image. `grid_steps`, the spacing
    of the labelled grid lines, says whether the two are describing the same
    square at all:

        map              published size/tile   client grid/steps
        cargo_port             1800 / 250          1800 / 250
        berlin                 1300 / 180          1300 / 180
        test-site_2271         1600 / 225          1700 / 225
        finland                1700 / 225          2048 / 275

    Where the tile matches, the client is measuring the same grid, so a
    disagreement over its size is wt-tools being wrong: Test-Site 2271 really is
    1700 m, which its own image confirms. Where the tile differs, the client is
    describing something else. Finland reports the whole 2048 m map, with
    `grid_zero` on the map's own corner, while its image covers 1700 m; taking
    that literally stretches the image by 20%.

    So the client wins only when it is talking about the same grid. Without a
    published size to compare against, the whole-map shape is the only tell.
    """
    grid = info.get("grid_size") or []
    if not grid or not grid[0]:
        return None
    size = float(grid[0])
    steps = (info.get("grid_steps") or [None])[0]

    tile = (map_meta or {}).get("tile_m")
    if tile and steps and abs(float(tile) - float(steps)) > 1.0:
        return None

    mn, mx = info.get("map_min") or [], info.get("map_max") or []
    zero = info.get("grid_zero") or []
    if len(mn) >= 2 and len(mx) >= 2 and len(zero) >= 2:
        if (abs((mx[0] - mn[0]) - size) < 1.0 and abs(zero[0] - mn[0]) < 1.0
                and abs(zero[1] - mx[1]) < 1.0):
            return None
    return size


def process(recording: Path, args) -> bool:
    source_path = recording / "source.json"
    if not source_path.exists():
        print(f"{recording}: no source.json, skipping "
              f"(only recordings made by auto_replay carry one)")
        return False
    source = json.loads(source_path.read_text(encoding="utf-8"))
    name = f"{layout_key(source['battleType'])[0]}-{layout_key(source['battleType'])[1]}"
    prefix = source.get("prefix") or f"{name}-{recording.name[:15]}"

    print(f"{recording}  [{source['battleType']}]")
    if args.skip_existing and (args.heatmaps / f"{prefix}.html").exists():
        print("  already processed")
        return True

    layout = ensure_layout(source, args.levels)
    if layout is None:
        print("  no layout geometry; cannot place anything on a map")
        return False

    map_dir = ensure_map(source, args.maps)

    info = json.loads((recording / "map_info.json").read_text(encoding="utf-8"))
    meta_path = (map_dir / "map.json") if map_dir else None
    map_meta = (json.loads(meta_path.read_text(encoding="utf-8"))
                if meta_path and meta_path.exists() else None)
    size_m = client_grid_size(info, map_meta)

    if map_dir is None:
        # wt-tools publishes 62 maps, not all of them. The client reports the
        # playable square as grid_size, and its centre matches the mission's
        # battle area, so the geometry survives without an image.
        if size_m is None:
            # With no image there is no published size to protect, so even the
            # whole-map grid is better than nothing: a wider view, still exact.
            grid = info.get("grid_size") or []
            size_m = float(grid[0]) if grid and grid[0] else None
        if size_m:
            print(f"  no map image published; using the client's "
                  f"{size_m:.0f} m grid instead")
        else:
            print("  no map image and no usable grid size; cannot continue")
            return False

    speed = source.get("speed", 1)
    group = battle_group(recording)
    if len(group) > 1:
        print(f"  continues into {len(group) - 1} more folder(s): "
              + ", ".join(d.name for d in group[1:]))
    result = run([sys.executable, "tools/build_tracks.py", *group, "--speed", speed],
                 quiet=True)
    for line in result.stdout.splitlines()[1:4]:
        print(" ", line.strip())
    if not (recording / "tracks.csv").exists():
        print("  no tracks produced")
        return False

    args.heatmaps.mkdir(parents=True, exist_ok=True)
    plot = [sys.executable, "tools/plot_tracks.py", recording, "--layout", layout,
            "--out", args.heatmaps / f"{prefix}-tracks.svg"]
    if map_dir:
        plot += ["--map", map_dir]
    if size_m:
        plot += ["--size", size_m]
    run(plot, what="track plot")

    tracks = recording / "tracks.csv"
    made = 0
    for vclass in CLASSES:
        cmd = [sys.executable, "heatmap/generate.py", tracks,
               "--layout", layout, "--out", args.heatmaps / f"{prefix}-{vclass}.png"]
        if map_dir:
            cmd += ["--map", map_dir]
        if size_m:
            cmd += ["--size", size_m]
        if vclass != "all":
            cmd += ["--class", vclass]
        made += run(cmd, what=f"heatmap {vclass}").returncode == 0
    for weighting in WEIGHTINGS:
        cmd = [sys.executable, "heatmap/generate.py", tracks, "--layout", layout,
               "--mode", weighting,
               "--out", args.heatmaps / f"{prefix}-{weighting}.png"]
        if map_dir:
            cmd += ["--map", map_dir]
        if size_m:
            cmd += ["--size", size_m]
        made += run(cmd, what=f"heatmap {weighting}").returncode == 0
    if made == 0:
        print("  no heatmaps produced")
        return False

    viewer = args.heatmaps / f"{prefix}.html"
    viewer_dir = map_dir or (args.maps / layout_key(source["battleType"])[0]
                             / wt_tools_mode(layout_key(source["battleType"])[1]))
    viewer_dir.mkdir(parents=True, exist_ok=True)
    if run([sys.executable, "tools/build_viewer.py", viewer_dir, "--heatmaps", args.heatmaps,
            "--prefix", prefix, "--out", viewer], what="viewer").returncode != 0:
        return False
    print(f"  {made} heatmaps -> {viewer}")
    return True


def expand(paths: list[Path]) -> list[Path]:
    """Expand wildcards ourselves.

    bash expands `data/live/*/` before the script sees it; PowerShell and cmd
    do not, and pass the literal string. Doing it here makes the same command
    work in any shell.
    """
    out: list[Path] = []
    for item in paths:
        text = str(item)
        if any(ch in text for ch in "*?["):
            matched = sorted(Path().glob(text.replace("\\", "/").rstrip("/")))
            out.extend(m for m in matched if m.is_dir())
        else:
            out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recordings", nargs="*", type=Path,
                        help="recording directories; omit when using --newest")
    parser.add_argument("--levels", type=Path, default=Path("data/levels"))
    parser.add_argument("--maps", type=Path, default=Path("data/maps"))
    parser.add_argument("--heatmaps", type=Path, default=Path("data/heatmaps"))
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--newest", type=int, metavar="N",
                        help="ignore the paths given and process the N most recent "
                             "recordings in data/live")
    args = parser.parse_args()
    args.recordings = expand(args.recordings)
    if args.newest:
        folders = [d for d in Path("data/live").iterdir() if d.is_dir()]
        args.recordings = sorted(folders, key=lambda d: d.stat().st_mtime,
                                 reverse=True)[:args.newest]
    if not args.recordings:
        parser.error("give one or more recording directories, or use --newest N")

    done = 0
    for recording in args.recordings:
        if recording.is_dir() and process(recording, args):
            done += 1
    print(f"\n{done} of {len(args.recordings)} recordings processed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
