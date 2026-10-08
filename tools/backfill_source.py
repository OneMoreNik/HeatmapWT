"""Work out which battle each recording came from, and write it down.

A recording is just map positions; nothing in it says which map or layout that
was. `auto_replay.py` writes a `source.json` beside the captures it makes. A
capture recorded while actually playing has nobody to write one, so this pairs
it with a replay afterwards.

Two ways, tried in order. A folder named after a replay's timestamp, which
`auto_replay` produces, is matched on that. Anything else is matched on time:
every battle leaves a replay whose file is written when it ends and whose header
gives its length, so the battle started at (write time - length), and a recording
of it starts within seconds of that. Across eleven battles the right replay was
never more than 8 seconds out and the nearest wrong one never closer than 164,
so the two are not easily confused -- but a match that is not clearly better
than the runner-up is refused rather than guessed.

    python tools/backfill_source.py data/live/*/
    python tools/backfill_source.py data/live/<one> --for-replay 19.31.23
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from process_recording import battle_group  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# auto_replay labels a recording with the replay's timestamp, digits only:
# "#2026.10.06 19.21.50.wrpl" becomes "20261006-192150".
LABEL = re.compile(r"(\d{8})-(\d{6})")

# A recording folder is named for when it started, in UTC.
FOLDER_STAMP = re.compile(r"^(\d{8})-(\d{6})")

# How far a recording's start may sit from a battle's, and how much closer the
# winner has to be than the next candidate. Observed: 8 s out at worst, with the
# runner-up never nearer than 164 s.
MATCH_WINDOW_S = 120.0
MIN_MARGIN_S = 60.0

# Hangar and air maps report grids of 32768 m and up. They are not battles.
MAX_GROUND_GRID_M = 8000.0


def replay_stamp(path: Path) -> str | None:
    digits = re.search(r"(\d{4})\.(\d{2})\.(\d{2})[ _](\d{2})\.(\d{2})\.(\d{2})", path.name)
    if not digits:
        return None
    y, mo, d, h, mi, s = digits.groups()
    return f"{y}{mo}{d}-{h}{mi}{s}"


def folder_start(recording: Path) -> float | None:
    """When the recording began, as seconds since the epoch."""
    match = FOLDER_STAMP.match(recording.name)
    if not match:
        return None
    stamp = datetime.datetime.strptime(match.group(0), "%Y%m%d-%H%M%S")
    return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()


def is_ground_battle(recording: Path) -> bool:
    """Whether this recording is a tank battle rather than the hangar or a plane."""
    info_path = recording / "map_info.json"
    if not info_path.exists():
        return False
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if info.get("hud_type") == 0:
        return False
    grid = info.get("grid_size") or []
    return bool(grid) and float(grid[0]) <= MAX_GROUND_GRID_M


def read_replays(folders: list[Path], wtresults: Path) -> dict[str, dict]:
    """Every replay on disk, with its battle and when that battle started.

    The same replay often sits in both the game's folder and this project's,
    and copying it there gave the copy a new modification time. The oldest copy
    is the original, and only the original says when the battle ended.
    """
    paths: dict[str, Path] = {}
    for folder in folders:
        for replay in folder.glob("*.wrpl") if folder.is_dir() else []:
            seen = paths.get(replay.name)
            if seen is None or replay.stat().st_mtime < seen.stat().st_mtime:
                paths[replay.name] = replay
    if not paths or not wtresults.exists():
        return {}

    result = subprocess.run([str(wtresults), "-json", *(str(p) for p in paths.values())],
                            capture_output=True, text=True, timeout=300,
                            encoding="utf-8", errors="replace")
    try:
        battles = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    out: dict[str, dict] = {}
    for battle in battles:
        name = Path(battle.get("file", "").replace("\\", "/")).name
        path = paths.get(name)
        if path is None or not battle.get("battleType"):
            continue
        out[name] = {
            "path": path,
            "started": path.stat().st_mtime - float(battle.get("timePlayed") or 0.0),
            "battle": battle,
        }
    return out


def pair_by_time(recording: Path, replays: dict[str, dict]) -> tuple[Path, dict] | None:
    """The replay of the battle this recording was made during, or None.

    Refused when two replays are nearly equally plausible: a wrong pairing
    silently mislabels a map, which is worse than asking for --for-replay.
    """
    started = folder_start(recording)
    if started is None or not replays:
        return None
    ranked = sorted((abs(started - entry["started"]), name)
                    for name, entry in replays.items())
    best_gap, best_name = ranked[0]
    if best_gap > MATCH_WINDOW_S:
        return None
    if len(ranked) > 1 and ranked[1][0] - best_gap < MIN_MARGIN_S:
        return None
    return replays[best_name]["path"], replays[best_name]["battle"]


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
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recordings", nargs="+", type=Path)
    parser.add_argument("--replays", type=Path, nargs="*",
                        default=[Path("replays"), Path("D:/Games/WarThunder/Replays")])
    parser.add_argument("--wtresults", type=Path, default=Path("bin/wtresults.exe"))
    parser.add_argument("--speed", type=int, default=1,
                        help="playback speed these recordings were made at")
    parser.add_argument("--for-replay",
                        help="name one replay explicitly, as a filename substring, "
                             "when the automatic pairing refuses to choose")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing source.json")
    args = parser.parse_args()
    args.recordings = sorted(expand(args.recordings))

    by_stamp: dict[str, Path] = {}
    for folder in args.replays:
        for replay in folder.glob("*.wrpl") if folder.is_dir() else []:
            stamp = replay_stamp(replay)
            if stamp:
                by_stamp.setdefault(stamp, replay)
    replays = read_replays(args.replays, args.wtresults)

    written = skipped = unmatched = 0
    # A battle whose map_generation ticked mid-way is several folders. Only the
    # first is labelled; process_recording rejoins the rest onto it, and
    # labelling them too would process the same battle twice.
    claimed: set[str] = set()

    for recording in args.recordings:
        if not recording.is_dir() or recording.name in claimed:
            continue
        target = recording / "source.json"
        if target.exists() and not args.force:
            continue
        if not is_ground_battle(recording):
            skipped += 1
            continue

        replay = battle = None
        if args.for_replay:
            named = [r for r in by_stamp.values()
                     if args.for_replay.lower() in r.name.lower()]
            if len(named) != 1:
                print(f"  {args.for_replay!r} matches {len(named)} replays; "
                      f"be more specific")
                continue
            replay = named[0]
        else:
            # auto_replay puts the replay's timestamp in the folder name.
            match = LABEL.search(recording.name[16:])
            if match:
                replay = by_stamp.get(match.group(0))
            if replay is None:
                paired = pair_by_time(recording, replays)
                if paired is None:
                    print(f"  {recording.name}: no replay matches the time it was "
                          f"recorded. Pass --for-replay <filename> if you know it")
                    unmatched += 1
                    continue
                replay, battle = paired

        label = replay_stamp(replay) or recording.name
        source = {"replay": str(replay), "label": label,
                  "speed": args.speed, "prefix": label}
        if battle is None:
            entry = replays.get(replay.name)
            battle = entry["battle"] if entry else None
        if battle is None and args.wtresults.exists():
            result = subprocess.run([str(args.wtresults), "-json", str(replay)],
                                    capture_output=True, text=True, timeout=60,
                                    encoding="utf-8", errors="replace")
            try:
                battle = json.loads(result.stdout)[0]
            except (json.JSONDecodeError, IndexError):
                battle = None
        if battle is None:
            print(f"  {recording.name}: could not read {replay.name}")
            continue
        try:
            source.update({
                "level": battle["level"], "mission": battle["mission"],
                "battleType": battle["battleType"], "sessionId": battle["sessionId"],
                "author": battle["author"], "authorTeam": battle["authorTeam"],
            })
        except KeyError:
            print(f"  {recording.name}: {replay.name} has no battle in it")
            continue

        target.write_text(json.dumps(source, indent=2), encoding="utf-8")
        for follower in battle_group(recording)[1:]:
            claimed.add(follower.name)
        print(f"  {recording.name} -> {source['battleType']}")
        written += 1

    print(f"{written} source.json written"
          + (f", {skipped} hangar or air recordings ignored" if skipped else "")
          + (f", {unmatched} unmatched" if unmatched else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
