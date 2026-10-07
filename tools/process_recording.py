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
    """Map image for this layout, downloaded from wt-tools if absent."""
    map_name, layout = layout_key(source["battleType"])
    mode = wt_tools_mode(layout)
    path = out / map_name / mode
    if (path / "map.json").exists():
        return path
    print(f"  fetching the map image for {map_name}/{mode}")
    run([sys.executable, "collector/fetch_maps.py", map_name, "--mode", mode,
         "--out", out], quiet=True)
    return path if (path / "map.json").exists() else None


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
    size_m = None
    if map_dir is None:
        # wt-tools publishes 62 maps, not all of them. The client reports the
        # playable square as grid_size, and its centre matches the mission's
        # battle area, so the geometry survives without an image.
        info = json.loads((recording / "map_info.json").read_text(encoding="utf-8"))
        grid = info.get("grid_size") or []
        if len(grid) >= 1 and grid[0]:
            size_m = float(grid[0])
            print(f"  no map image published for this map; using the client's "
                  f"{size_m:.0f} m grid instead")
        else:
            print("  no map image and no grid size; cannot continue")
            return False

    speed = source.get("speed", 1)
    result = run([sys.executable, "tools/build_tracks.py", recording, "--speed", speed],
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
    run(plot, what="track plot")

    tracks = recording / "tracks.csv"
    made = 0
    for vclass in CLASSES:
        cmd = [sys.executable, "heatmap/generate.py", tracks,
               "--layout", layout, "--out", args.heatmaps / f"{prefix}-{vclass}.png"]
        cmd += ["--map", map_dir] if map_dir else ["--size", size_m]
        if vclass != "all":
            cmd += ["--class", vclass]
        made += run(cmd, what=f"heatmap {vclass}").returncode == 0
    for weighting in WEIGHTINGS:
        cmd = [sys.executable, "heatmap/generate.py", tracks, "--layout", layout,
               "--mode", weighting,
               "--out", args.heatmaps / f"{prefix}-{weighting}.png"]
        cmd += ["--map", map_dir] if map_dir else ["--size", size_m]
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
