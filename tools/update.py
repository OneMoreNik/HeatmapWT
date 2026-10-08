"""Turn whatever has been recorded into up-to-date heatmaps. One command.

Everything between finishing a battle and having a heatmap to look at:

    1. pair each new capture with the battle it was recorded during
    2. build that battle's tracks, plots and heatmaps
    3. combine every battle on a map into the heatmap the overlay reads

Running the three by hand meant chaining them with `&&`, which Windows
PowerShell does not accept, so this exists to be the one command that works in
any shell.

    python tools/update.py
    python tools/update.py --by-map     # also combine a map's layouts together
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UNMATCHED = re.compile(r"^\s*(\d{8}-\d{6}\S*):\s*no replay matches")


def step(number: int, total: int, title: str, args: list) -> tuple[bool, str]:
    print(f"\n[{number}/{total}] {title}")
    print("-" * 60)
    result = subprocess.run([str(a) for a in args], text=True,
                            encoding="utf-8", errors="replace",
                            capture_output=True)
    output = (result.stdout or "") + (result.stderr or "")
    for line in output.splitlines():
        print(f"  {line}")
    if result.returncode != 0:
        print(f"  ! {Path(str(args[1])).name} exited {result.returncode}")
    return result.returncode == 0, output


def report_unmatched(paired: str) -> None:
    """Say which captures are still coming and which never will be.

    A capture from today is probably a battle whose replay is not written yet,
    because the client writes one when you leave. An older one never got a
    replay and never will, so telling the user to run this again about all of
    them would be wrong.
    """
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    recent, stale = [], []
    for line in paired.splitlines():
        found = UNMATCHED.match(line)
        if found:
            (recent if found.group(1).startswith(today) else stale).append(found.group(1))

    if recent:
        print(f"{len(recent)} capture(s) recorded today have no replay yet. One is "
              f"written when you")
        print("leave a battle, so run this again after that:")
        for name in recent[:5]:
            print(f"  {name}")
    if stale:
        print(f"{len(stale)} older capture(s) never got a replay and will stay "
              f"unmatched. To name")
        print("one yourself:")
        print("  python tools/backfill_source.py data/live/<folder> --for-replay <name>")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", type=Path, default=Path("data/live"))
    parser.add_argument("--by-map", action="store_true",
                        help="also build one heatmap per map, mixing its layouts")
    parser.add_argument("--rebuild", action="store_true",
                        help="redo battles that already have heatmaps")
    args = parser.parse_args()

    if not args.live.is_dir():
        print(f"{args.live} does not exist. Record a battle first:")
        print("  python collector/sample_map.py --hz 20 --label live")
        return 1

    here = Path(__file__).resolve().parent
    total = 4 if args.by_map else 3

    ok, paired = step(1, total, "pairing new captures with their battles",
                      [sys.executable, here / "backfill_source.py", args.live / "*"])
    if not ok:
        return 1

    process = [sys.executable, here / "process_recording.py", args.live / "*"]
    if not args.rebuild:
        process.append("--skip-existing")
    step(2, total, "building tracks and per-battle heatmaps", process)

    step(3, total, "combining every battle on each map and layout",
         [sys.executable, here / "build_map.py"])
    if args.by_map:
        step(4, total, "combining each map's layouts together",
             [sys.executable, here / "build_map.py", "--by-map"])

    print("\n" + "=" * 60)
    report_unmatched(paired)
    print("Done. What you have, and which maps repeat:")
    print("  python tools/build_map.py --list")
    print("Then leave the overlay running while you play:")
    print("  python overlay/overlay.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
