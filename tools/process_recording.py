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


def run(args: list[str], quiet: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], capture_output=quiet, text=True,
                          encoding="utf-8", errors="replace")


def layout_key(battle_type: str) -> tuple[str, str]:
    """"berlin_Conq2" -> ("berlin", "conq2"); the map and its layout."""
    map_name, _, layout = battle_type.partition("_")
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
    map_dir = ensure_map(source, args.maps)
    if layout is None or map_dir is None:
        print("  missing layout or map; cannot continue")
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
    run([sys.executable, "tools/plot_tracks.py", recording, "--layout", layout,
         "--map", map_dir, "--out", args.heatmaps / f"{prefix}-tracks.svg"])

    tracks = recording / "tracks.csv"
    for vclass in CLASSES:
        cmd = [sys.executable, "heatmap/generate.py", tracks, "--map", map_dir,
               "--layout", layout, "--out", args.heatmaps / f"{prefix}-{vclass}.png"]
        if vclass != "all":
            cmd += ["--class", vclass]
        run(cmd)
    for weighting in WEIGHTINGS:
        run([sys.executable, "heatmap/generate.py", tracks, "--map", map_dir,
             "--layout", layout, "--mode", weighting,
             "--out", args.heatmaps / f"{prefix}-{weighting}.png"])

    viewer = args.heatmaps / f"{prefix}.html"
    run([sys.executable, "tools/build_viewer.py", map_dir, "--heatmaps", args.heatmaps,
         "--prefix", prefix, "--out", viewer])
    print(f"  -> {viewer}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recordings", nargs="+", type=Path)
    parser.add_argument("--levels", type=Path, default=Path("data/levels"))
    parser.add_argument("--maps", type=Path, default=Path("data/maps"))
    parser.add_argument("--heatmaps", type=Path, default=Path("data/heatmaps"))
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()

    done = 0
    for recording in args.recordings:
        if recording.is_dir() and process(recording, args):
            done += 1
    print(f"\n{done} of {len(args.recordings)} recordings processed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
