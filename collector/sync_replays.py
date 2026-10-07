"""Copy replays you want to keep into the game's replay folder.

The game writes replays into its own folder and prunes old ones, so anything
worth keeping is copied into this repo. To play one back, it has to be in the
game's folder again. This copies in whatever is missing and leaves everything
else alone — it never deletes from either side.

    python collector/sync_replays.py --list
    python collector/sync_replays.py
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# Replay names and nicknames are not representable in the console's default
# Windows codepage, so force UTF-8 rather than crashing on a Cyrillic tag.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


REPO_REPLAYS = Path("replays")
GAME_REPLAYS = Path("D:/Games/WarThunder/Replays")


# Autosaved replays are named "#YYYY.MM.DD HH.MM.SS", but a renamed one keeps
# its date without the "#". Sorting the raw filename puts renamed replays first
# because "#" sorts before a digit, while the client orders by battle date.
_DATE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})[ _](\d{2})\.(\d{2})\.(\d{2})")


def replay_date(path: Path) -> tuple:
    """Sort key matching the client's list: newest battle first."""
    match = _DATE.search(path.name)
    if match:
        return tuple(int(g) for g in match.groups())
    # No date in the name: fall back to the file's own timestamp.
    return (0, 0, 0, 0, 0, int(path.stat().st_mtime))


def listed_order(folder: Path) -> list[Path]:
    """Replays as the client lists them, newest first."""
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.wrpl"), key=replay_date, reverse=True)


def replays_in(folder: Path) -> dict[str, Path]:
    return {p.name: p for p in listed_order(folder)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_REPLAYS)
    parser.add_argument("--game", type=Path, default=GAME_REPLAYS)
    parser.add_argument("--list", action="store_true", help="show both sides, copy nothing")
    args = parser.parse_args()

    if not args.game.is_dir():
        raise SystemExit(f"game replay folder not found: {args.game}")
    args.repo.mkdir(parents=True, exist_ok=True)

    repo = replays_in(args.repo)
    game = replays_in(args.game)
    missing = [name for name in repo if name not in game]

    print(f"repo {args.repo}: {len(repo)} replays")
    print(f"game {args.game}: {len(game)} replays")

    if args.list:
        # The game lists newest first, which is the order the automation
        # counts rows in, so show it the same way.
        print("\nin the game folder, newest first (row order in the replay list):")
        for i, path in enumerate(listed_order(args.game)):
            here = " [also in repo]" if path.name in repo else ""
            print(f"  {i:3d}  {path.name}{here}")
        if missing:
            print(f"\n{len(missing)} in the repo are not in the game folder:")
            for name in sorted(missing):
                print(f"  {name}")
        return 0

    if not missing:
        print("nothing to copy")
        return 0

    for name in sorted(missing):
        target = args.game / name
        shutil.copy2(repo[name], target)
        print(f"  copied {name} ({target.stat().st_size:,} B)")
    print(f"{len(missing)} copied; the game lists them newest first")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
