"""Download the server-side replay for a session, part by part.

A client ``.wrpl`` only contains what your own client was sent.  The server
replay for the same session has every vehicle on both teams, split into
roughly 30-second parts:

    https://wt-game-replays.warthunder.com/<session hex>/0000.wrpl

That host 302s to a Gaijin CDN, so redirects must be followed.  Parts are
not always contiguous (observed numbering is 0, 1, 3, 5, ...), so a few
misses in a row are tolerated before deciding the session has ended.

    python collector/download_server_replay.py replays/*.wrpl
    python collector/download_server_replay.py --session 127b0049000219ad
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from heatmapwt import wrpl

BASE_URL = "https://wt-game-replays.warthunder.com"
USER_AGENT = "HeatmapWT/0.1 (personal replay archiving)"
MAX_CONSECUTIVE_MISSES = 4
MAX_PARTS = 200


def part_url(session_hex: str, part: int) -> str:
    return f"{BASE_URL}/{session_hex}/{part:04d}.wrpl"


def fetch(url: str, timeout: float = 60.0) -> bytes | None:
    """Body of ``url``, or ``None`` if the archive does not have it."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        if err.code in (403, 404):
            return None
        raise


def download_session(session_hex: str, out_root: Path) -> list[Path]:
    out_dir = out_root / session_hex
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    misses = 0

    for part in range(MAX_PARTS):
        target = out_dir / f"{part:04d}.wrpl"
        if target.exists() and target.stat().st_size > 0:
            print(f"  part {part:4d}  cached       {target.stat().st_size:>10,} B")
            written.append(target)
            misses = 0
            continue

        body = fetch(part_url(session_hex, part))
        if body is None:
            misses += 1
            if misses >= MAX_CONSECUTIVE_MISSES:
                break
            continue

        target.write_bytes(body)
        print(f"  part {part:4d}  downloaded   {len(body):>10,} B")
        written.append(target)
        misses = 0

    total = sum(p.stat().st_size for p in written)
    print(f"  {len(written)} parts, {total:,} bytes -> {out_dir}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replays", nargs="*", type=Path, help="client .wrpl files to take session ids from")
    parser.add_argument("--session", action="append", default=[], help="session id in hex, instead of a replay file")
    parser.add_argument("--out", type=Path, default=Path("replays/server"))
    args = parser.parse_args()

    sessions: list[tuple[str, str]] = [(s, s) for s in args.session]
    for path in args.replays:
        header = wrpl.read_header(path)
        label = f"{path.name} ({header.map_name}/{header.layout})"
        sessions.append((header.session_hex, label))
    if not sessions:
        parser.error("give at least one replay file or --session")

    for session_hex, label in sessions:
        print(f"{label} -> session {session_hex}")
        download_session(session_hex, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
