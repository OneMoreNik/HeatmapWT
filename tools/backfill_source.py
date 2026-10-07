"""Attach a source.json to recordings made before auto_replay wrote one.

A recording is just map positions; it does not say which battle it came from,
so the analysis cannot work out the map or layout. auto_replay now writes that
alongside each capture. This reconstructs it for older recordings by matching
the folder name, which carries the replay's timestamp, against the replays on
disk.

    python tools/backfill_source.py data/live/*/
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# auto_replay labels a recording with the replay's timestamp, digits only:
# "#2026.10.06 19.21.50.wrpl" becomes "20261006-192150".
LABEL = re.compile(r"(\d{8})-(\d{6})")


def replay_stamp(path: Path) -> str | None:
    digits = re.search(r"(\d{4})\.(\d{2})\.(\d{2})[ _](\d{2})\.(\d{2})\.(\d{2})", path.name)
    if not digits:
        return None
    y, mo, d, h, mi, s = digits.groups()
    return f"{y}{mo}{d}-{h}{mi}{s}"


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
    parser.add_argument("recordings", nargs="+", type=Path)
    parser.add_argument("--replays", type=Path, nargs="*",
                        default=[Path("replays"), Path("D:/Games/WarThunder/Replays")])
    parser.add_argument("--wtresults", type=Path, default=Path("bin/wtresults.exe"))
    parser.add_argument("--speed", type=int, default=1,
                        help="playback speed these recordings were made at")
    parser.add_argument("--for-replay",
                        help="name the replay explicitly, matched as a filename "
                             "substring. Needed for a live capture, whose folder "
                             "carries its own label rather than a replay timestamp")
    parser.add_argument("--force", action="store_true", help="overwrite an existing source.json")
    args = parser.parse_args()
    args.recordings = expand(args.recordings)

    by_stamp: dict[str, Path] = {}
    for folder in args.replays:
        for replay in folder.glob("*.wrpl") if folder.is_dir() else []:
            stamp = replay_stamp(replay)
            if stamp:
                by_stamp.setdefault(stamp, replay)

    written = 0
    for recording in args.recordings:
        if not recording.is_dir():
            continue
        target = recording / "source.json"
        if target.exists() and not args.force:
            continue
        if args.for_replay:
            named = [r for r in by_stamp.values()
                     if args.for_replay.lower() in r.name.lower()]
            if len(named) != 1:
                print(f"  {args.for_replay!r} matches {len(named)} replays; "
                      f"be more specific")
                continue
            replay, label = named[0], replay_stamp(named[0]) or recording.name
        else:
            match = LABEL.search(recording.name[16:])  # skip the capture's own timestamp
            replay = by_stamp.get(match.group(0)) if match else None
            if replay is None:
                print(f"  {recording.name}: no replay timestamp in the folder name. "
                      f"For a live capture, pass --for-replay <filename>")
                continue
            label = match.group(0)

        source = {"replay": str(replay), "label": label,
                  "speed": args.speed, "prefix": label}
        if args.wtresults.exists():
            result = subprocess.run([str(args.wtresults), "-json", str(replay)],
                                    capture_output=True, text=True, timeout=60,
                                    encoding="utf-8", errors="replace")
            try:
                battle = json.loads(result.stdout)[0]
                source.update({
                    "level": battle["level"], "mission": battle["mission"],
                    "battleType": battle["battleType"], "sessionId": battle["sessionId"],
                    "author": battle["author"], "authorTeam": battle["authorTeam"],
                })
            except (json.JSONDecodeError, IndexError, KeyError):
                print(f"  {recording.name}: could not read {replay.name}")
                continue
        target.write_text(json.dumps(source, indent=2), encoding="utf-8")
        print(f"  {recording.name} -> {source.get('battleType', '?')}")
        written += 1

    print(f"{written} source.json written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
