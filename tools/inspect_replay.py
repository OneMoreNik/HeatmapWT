"""Print what we can currently read out of a .wrpl file.

    python tools/inspect_replay.py replays/*.wrpl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from heatmapwt import roster, wrpl


def describe(path: Path) -> None:
    header = wrpl.read_header(path)
    body = wrpl.read_body(path)
    models = roster.vehicle_models(body)

    print(f"{path.name}")
    print(f"  map / layout   {header.map_name} / {header.layout}  ({header.battle_type})")
    print(f"  conditions     {header.environment}, {header.visibility}")
    print(f"  started        {header.start_time:%Y-%m-%d %H:%M:%S} UTC")
    print(f"  replay version {header.version}")
    print(f"  session id     {header.session_hex}")
    print(f"  server replay  {header.server_replay_url()}")
    print(f"  packet stream  {len(body):,} bytes decompressed")
    print(f"  vehicles       {len(models)}")
    for nation, group in sorted(roster.by_nation(models).items()):
        print(f"    {nation:5} {', '.join(group)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replays", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.replays:
        describe(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
