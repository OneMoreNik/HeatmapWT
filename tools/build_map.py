"""Combine every battle recorded on a map into one heatmap per map and layout.

`process_recording.py` turns one capture into one battle's heatmap, which is
the thing to look at when checking that a capture worked. This is the thing to
look at before playing: every battle on `berlin_Conq2` stacked into a single
density, so a route that two teams took reads as a route rather than as two
one-off tracks.

A layout is the unit, not a map. `berlin_Dom` and `berlin_Conq2` have different
capture points and often a different playable square, so stacking them would
average two different battles together.

    python tools/build_map.py                 # every layout with a recording
    python tools/build_map.py --min-battles 2 # only the ones worth reading
    python tools/build_map.py berlin_Conq2    # just this one
    python tools/build_map.py --list          # what is on disk, and nothing else
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from process_recording import (  # noqa: E402
    layout_key, render_set, resolve_geometry)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Two squares this far apart are not the same battlefield.
SQUARE_TOLERANCE_M = 5.0


def fingerprint(info: dict) -> list[float] | None:
    """Everything `map_info` says about the shape of the map, as one key.

    Matching a live map to a heatmap by the centre and size of its square alone
    is not enough to tell maps apart: Attica's battle area sits close enough to
    Cargo Port's that it matched, and the overlay confidently drew the wrong
    map. The grid origin, both grid sides, the grid step and the map extent
    together are distinctive -- across every recording made so far no two
    different maps share them, and the only collisions are between layouts of
    one map, which is a known ambiguity handled elsewhere.
    """
    parts = []
    for key in ("grid_zero", "grid_size", "grid_steps", "map_min", "map_max"):
        value = info.get(key) or []
        if len(value) < 2:
            return None
        parts += [round(float(value[0]), 2), round(float(value[1]), 2)]
    return parts


# A hangar or air-map grid, not a ground battle: 32768 m and up.
MAX_GROUND_GRID_M = 8000.0


def fingerprints_for(live: Path, battle_types: list[str]) -> list[list[float]]:
    """Every grid these battle types have ever been seen with.

    Deduplicating captures is right for counting battles and weighting the
    density, but wrong for recognising a map later: a capture can open on a
    transient grid before the client settles, and one Berlin Conquest-2 capture
    does exactly that. Any reading the client has produced for this map is worth
    recognising, so every capture contributes, including ones whose tracks were
    never built.
    """
    out: list[list[float]] = []
    for source_path in sorted(live.glob("*/source.json")):
        source = json.loads(source_path.read_text(encoding="utf-8"))
        if source.get("battleType") not in battle_types:
            continue
        info_path = source_path.parent / "map_info.json"
        if not info_path.exists():
            continue
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if info.get("hud_type") == 0:
            continue
        grid = info.get("grid_size") or []
        if not grid or float(grid[0]) > MAX_GROUND_GRID_M:
            continue
        key = fingerprint(info)
        if key and key not in out:
            out.append(key)
    return out


def describe(size_m: float | None, centre: tuple[float, float] | None) -> str:
    if size_m is None:
        return "the published size on the mission's battle area"
    return f"{size_m:.0f} m at ({centre[0]:.0f}, {centre[1]:.0f})"


def same_square(size_a, centre_a, size_b, centre_b) -> bool:
    """Whether two battles were drawn on the same square.

    None means "no override": the published size, centred on the mission's
    battle area. Two battles that both say that agree, because the answer comes
    from the same files either way.
    """
    if size_a is None or size_b is None:
        return (size_a is None) == (size_b is None)
    if abs(size_a - size_b) > SQUARE_TOLERANCE_M:
        return False
    if centre_a is None or centre_b is None:
        return (centre_a is None) == (centre_b is None)
    return max(abs(a - b) for a, b in zip(centre_a, centre_b)) <= SQUARE_TOLERANCE_M


def collect(live: Path) -> dict[str, list[tuple[Path, dict]]]:
    """Distinct battles, grouped by battle type, oldest first.

    Deduplicated on `sessionId`, because the same battle can be captured more
    than once -- replaying a replay to test a change does exactly that, and the
    two Berlin Conquest-2 captures on disk are one battle recorded twice. Left
    in, a battle would count twice towards the confidence the viewer reports and
    weigh twice as heavily in the density. The longest capture of a session wins.
    """
    best: dict[tuple[str, str], tuple[Path, dict, int]] = {}
    for source_path in sorted(live.glob("*/source.json")):
        recording = source_path.parent
        tracks = recording / "tracks.csv"
        if not tracks.exists():
            continue
        source = json.loads(source_path.read_text(encoding="utf-8"))
        battle_type = source.get("battleType")
        if not battle_type:
            continue
        # No session id (a hand-made source.json) means it cannot be shown to be
        # a duplicate, so it is kept on its own.
        key = (battle_type, source.get("sessionId") or str(recording))
        samples = tracks.stat().st_size
        if key not in best or samples > best[key][2]:
            best[key] = (recording, source, samples)

    groups: dict[str, list[tuple[Path, dict]]] = defaultdict(list)
    for (battle_type, _), (recording, source, _) in sorted(best.items()):
        groups[battle_type].append((recording, source))
    return groups


def by_map(groups: dict[str, list[tuple[Path, dict]]]
           ) -> dict[str, list[tuple[Path, dict]]]:
    """Regroup layouts into whole maps.

    Layouts differ in their capture points and often in their playable square,
    so this mixes objectives: Domination fights over three points where Conquest
    fights over one. What survives is the terrain -- the roads people take, the
    ridges they stop behind -- which is most of what the map is worth knowing.
    Squares that disagree are dropped by `build`, so a layout played on a
    different part of the map cannot quietly contaminate the rest.
    """
    merged: dict[str, list[tuple[Path, dict]]] = defaultdict(list)
    for battle_type, members in groups.items():
        merged[layout_key(battle_type)[0]].extend(members)
    return merged


def build(key: str, members: list[tuple[Path, dict]], args,
          whole_map: bool = False) -> bool:
    # In --by-map the key is a bare map name, which must not go through
    # layout_key: that splits on the last underscore and would read
    # "middle_east" as the map "middle" with the layout "east".
    if whole_map:
        prefix = f"{key}-all"
    else:
        map_name, layout_name = layout_key(key)
        prefix = f"{map_name}-{layout_name}-all"
    battle_type = members[0][1]["battleType"]

    # Rebuilding every map after every battle costs 219 s across nineteen of
    # them and grows with the collection, while one battle changes exactly one
    # map. A heatmap newer than every track that went into it is already right.
    viewer = args.heatmaps / f"{prefix}.html"
    index = args.heatmaps / f"{prefix}.index.json"
    if not args.rebuild and viewer.exists() and index.exists():
        inputs = [recording / "tracks.csv" for recording, _ in members]
        newest = max((p.stat().st_mtime for p in inputs if p.exists()), default=0)
        if viewer.stat().st_mtime >= newest:
            return None

    print(f"{key}  ({len(members)} battle{'s' if len(members) != 1 else ''})")

    # Geometry comes from the first battle; the rest have to agree with it.
    # They always should, but a mission can be reworked between patches, and
    # silently averaging two different squares would be invisible in the output.
    first_recording, first_source = members[0]
    resolved = resolve_geometry(first_source, first_recording, args)
    if resolved is None:
        return False
    layout, map_dir, size_m, centre = resolved

    tracks = []
    for recording, source in members:
        other = resolve_geometry(source, recording, args)
        if other is None:
            continue
        _, _, other_size, other_centre = other
        if not same_square(size_m, centre, other_size, other_centre):
            print(f"  skipping {recording.name}: its square is "
                  f"{describe(other_size, other_centre)} rather than "
                  f"{describe(size_m, centre)}")
            continue
        tracks.append(recording / "tracks.csv")

    if not tracks:
        print("  nothing to combine")
        return False
    made = render_set(tracks, layout, map_dir, size_m, centre, prefix,
                      args.heatmaps, battle_type)
    if made:
        # What the client reported for each battle that went in, so the overlay
        # can recognise this map from the live endpoint rather than guessing
        # from the shape of its square.
        types = []
        for _, source in members:
            if source.get("battleType") not in types:
                types.append(source.get("battleType"))
        prints = fingerprints_for(args.live, types)
        (args.heatmaps / f"{prefix}.index.json").write_text(json.dumps({
            "prefix": prefix,
            "battle_types": types,
            "whole_map": whole_map,
            "fingerprints": prints,
        }, indent=2), encoding="utf-8")
        sidecar = args.heatmaps / f"{prefix}-all.json"
        if sidecar.exists():
            counts = json.loads(sidecar.read_text(encoding="utf-8")).get("counts", {})
            total, placed = counts.get("samples", 0), counts.get("placed", 0)
            if total:
                share = 100 * placed / total
                note = "" if share > 99.5 else "  (the rest falls outside this square)"
                print(f"  {counts.get('battles', 0)} battles, "
                      f"{counts.get('vehicles', 0)} vehicles, "
                      f"{counts.get('vehicle_seconds', 0) / 60:.0f} vehicle-minutes; "
                      f"{share:.1f}% of samples drawn{note}")
    return made > 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("battle_types", nargs="*",
                        help="e.g. berlin_Conq2; omit for all of them")
    parser.add_argument("--live", type=Path, default=Path("data/live"))
    parser.add_argument("--levels", type=Path, default=Path("data/levels"))
    parser.add_argument("--maps", type=Path, default=Path("data/maps"))
    parser.add_argument("--heatmaps", type=Path, default=Path("data/heatmaps"))
    parser.add_argument("--min-battles", type=int, default=1,
                        help="skip layouts with fewer battles than this")
    parser.add_argument("--list", action="store_true",
                        help="show what has been recorded, and build nothing")
    parser.add_argument("--rebuild", action="store_true",
                        help="redo every map, even ones whose battles have not "
                             "changed since they were last built")
    parser.add_argument("--by-map", action="store_true",
                        help="combine every layout of a map into one heatmap. Mixes "
                             "game modes, so the capture points no longer agree, but "
                             "the terrain does")
    args = parser.parse_args()

    groups = collect(args.live)
    if args.by_map:
        groups = by_map(groups)
    if not groups:
        print(f"no processed recordings in {args.live}. Record a battle, then run "
              f"tools/process_recording.py on it first.")
        return 1

    if args.list:
        print(f"{sum(len(v) for v in groups.values())} battles across "
              f"{len(groups)} layouts:\n")
        for battle_type in sorted(groups, key=lambda k: (-len(groups[k]), k)):
            members = groups[battle_type]
            minutes = 0.0
            for recording, _ in members:
                rows = (recording / "tracks.csv").read_text(encoding="utf-8").count("\n")
                minutes += rows / 60
            mark = "  <- worth reading" if len(members) >= 3 else ""
            print(f"  {battle_type:<24} {len(members):>2} battle"
                  f"{'s' if len(members) != 1 else ' '}{mark}")
        maps = by_map(groups)
        repeats = {k: v for k, v in maps.items() if len(v) > 1}
        if repeats and not args.by_map:
            print("\n  maps with more than one battle, across all their layouts "
                  "(--by-map combines these):")
            for name in sorted(repeats, key=lambda k: -len(repeats[k])):
                print(f"    {name:<22} {len(repeats[name])} battles")
        print("\nThe more battles on one layout, the more a hot spot means "
              "'people go here' rather than 'someone went here once'.")
        return 0

    wanted = args.battle_types or sorted(groups)
    built = fresh = 0
    for battle_type in wanted:
        members = groups.get(battle_type)
        if not members:
            print(f"{battle_type}: nothing recorded (have "
                  f"{', '.join(sorted(groups))})")
            continue
        if len(members) < args.min_battles:
            continue
        outcome = build(battle_type, members, args, args.by_map)
        if outcome is None:
            fresh += 1
        else:
            built += outcome
    summary = f"{built} combined heatmap{'s' if built != 1 else ''} in {args.heatmaps}"
    if fresh:
        summary += f", {fresh} already up to date"
    print("\n" + summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
