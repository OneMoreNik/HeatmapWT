"""Play replays back automatically and record the positions from each one.

Drives the client's replay browser with synthetic mouse clicks, using
`collector/ui_layout.json` for where things are. It never reads the screen, so
the interface language does not matter.

It does not try to pick a player to follow. The in-replay player list reorders
itself during a battle, so a row index cannot reliably select a named player —
and it does not need to: the heatmap wants every vehicle the client draws, not
one of them. A replay opens on its own author, so the highlighted track is
always that replay's recorder, which `bin/wtresults.exe` can name offline.

**This automates the replay viewer in the menus, never gameplay.** Do not run
it while a live battle is in progress; it checks and refuses.

    python collector/auto_replay.py --list
    python collector/auto_replay.py --rows 0 --dry-run
    python collector/auto_replay.py --rows 0,1,2 --speed 16

Stop it at any time by moving the mouse to the top-left corner of the screen,
or with Ctrl-C.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sample_map import Api, map_is_live  # noqa: E402
from sync_replays import listed_order  # noqa: E402

# Replay names and nicknames are not representable in the console's default
# Windows codepage, so force UTF-8 rather than crashing on a Cyrillic tag.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

user32 = ctypes.WinDLL("user32", use_last_error=True)

WINDOW_TITLE = "War Thunder"
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


class Screen:
    """Synthetic mouse input, with an abort corner and a dry-run mode."""

    def __init__(self, layout: dict, dry_run: bool):
        self.layout = layout
        self.dry_run = dry_run
        self.width = user32.GetSystemMetrics(0)
        self.height = user32.GetSystemMetrics(1)
        ref = layout.get("screen", {})
        self.scale_x = self.width / ref.get("width", self.width)
        self.scale_y = self.height / ref.get("height", self.height)

    def to_screen(self, point: dict) -> tuple[int, int]:
        return int(round(point["x"] * self.scale_x)), int(round(point["y"] * self.scale_y))

    def aborted(self) -> bool:
        """True when the pointer is parked in the top-left corner."""
        pos = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pos))
        return pos.x <= 2 and pos.y <= 2

    def click(self, point: dict, label: str = "") -> None:
        x, y = self.to_screen(point)
        note = point.get("note", label)
        if self.dry_run:
            print(f"    [dry-run] click ({x},{y}) {note}")
            return
        if self.aborted():
            raise KeyboardInterrupt("pointer parked in the top-left corner")
        ax = int(x * 65535 / max(self.width - 1, 1))
        ay = int(y * 65535 / max(self.height - 1, 1))
        for flags in (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                      MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
            event = INPUT(type=INPUT_MOUSE,
                          mi=MOUSEINPUT(ax, ay, 0, flags, 0, None))
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
            time.sleep(0.03)
        print(f"    click ({x},{y}) {note}")
        time.sleep(self.layout["timings"]["after_click_s"])


def focus_game() -> bool:
    handle = user32.FindWindowW(None, WINDOW_TITLE)
    if not handle:
        return False
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.SetForegroundWindow(handle)
    time.sleep(0.6)
    return True


def game_replays(folder: Path) -> list[Path]:
    """Replay files as the client lists them: newest first."""
    return listed_order(folder)


def describe(replay: Path, wtresults: Path) -> str:
    """One line about a replay, read from the file rather than the screen."""
    if not wtresults.exists():
        return replay.name
    try:
        out = subprocess.run([str(wtresults), "-json", str(replay)],
                             capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace")
        data = json.loads(out.stdout)
        if not data:
            return replay.name
        battle = data[0]
        players = battle.get("players", [])
        top = players[0]["name"] if players else "?"
        return (f"{replay.name}  {battle.get('battleType', '?')}  "
                f"{battle.get('timePlayed', 0):.0f}s  top: {top}")
    except Exception:
        return replay.name


def wait_for(api: Api, want_live: bool, timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if map_is_live(api.get("map_info.json")) == want_live:
            return True
        time.sleep(1.0)
    print(f"    timed out waiting for {what}")
    return False


def play_one(screen: Screen, api: Api, row: int, speed_clicks: int,
             label: str, record_hz: float, out_dir: Path) -> bool:
    layout = screen.layout
    timings = layout["timings"]
    hangar, dialog, view = layout["hangar"], layout["replay_dialog"], layout["replay_view"]

    listing = dialog["list"]
    if row >= listing["visible_rows"]:
        print(f"    row {row} is past the {listing['visible_rows']} visible rows; "
              f"scrolling is not implemented")
        return False

    screen.click(hangar["community_menu"])
    time.sleep(timings["menu_open_s"])
    screen.click(hangar["community_replays"])
    time.sleep(timings["dialog_open_s"])

    screen.click({"x": listing["x"],
                  "y": listing["first_y"] + row * listing["row_height"],
                  "note": f"replay row {row}"})
    screen.click(dialog["watch_button"])

    if screen.dry_run:
        print("    [dry-run] would wait for the battle, set speed, record, and exit")
        return True

    recorder = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("sample_map.py")),
         "--hz", str(record_hz), "--label", label, "--out", str(out_dir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for(api, True, timings["replay_load_timeout_s"], "the replay to load"):
            return False
        print("    replay running; setting playback speed")
        for _ in range(speed_clicks):
            screen.click(view["speed_up"])

        start = time.monotonic()
        while map_is_live(api.get("map_info.json")):
            if screen.aborted():
                raise KeyboardInterrupt("pointer parked in the top-left corner")
            time.sleep(2.0)
        print(f"    battle ended after {time.monotonic() - start:.0f}s of playback")
        time.sleep(timings["battle_end_grace_s"])
    finally:
        recorder.terminate()
        try:
            recorder.wait(timeout=20)
        except subprocess.TimeoutExpired:
            recorder.kill()
    return True


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, default=Path("D:/Games/WarThunder/Replays"))
    parser.add_argument("--layout", type=Path, default=here / "ui_layout.json")
    parser.add_argument("--wtresults", type=Path, default=Path("bin/wtresults.exe"))
    parser.add_argument("--out", type=Path, default=Path("data/live"))
    parser.add_argument("--rows", default="0", help="comma-separated row numbers, newest first")
    parser.add_argument("--speed", type=int, default=16,
                        help="playback speed; reached by doubling from 1x")
    parser.add_argument("--hz", type=float, default=20.0, help="recording rate")
    parser.add_argument("--list", action="store_true", help="list replays with their row numbers")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every click without sending any input")
    args = parser.parse_args()

    layout = json.loads(args.layout.read_text(encoding="utf-8"))
    replays = game_replays(args.game)
    if not replays:
        raise SystemExit(f"no replays in {args.game}")

    if args.list:
        print(f"{len(replays)} replays in {args.game}, newest first:")
        for i, replay in enumerate(replays[:layout['replay_dialog']['list']['visible_rows']]):
            print(f"  row {i}: {describe(replay, args.wtresults)}")
        if len(replays) > layout["replay_dialog"]["list"]["visible_rows"]:
            print(f"  ... {len(replays) - layout['replay_dialog']['list']['visible_rows']} more, "
                  f"past the visible rows")
        return 0

    api = Api()
    if not args.dry_run and map_is_live(api.get("map_info.json")):
        raise SystemExit("a battle or replay is already running; "
                         "return to the hangar first")

    if not args.dry_run and not focus_game():
        raise SystemExit(f"could not find a window titled {WINDOW_TITLE!r}")

    speed_clicks = max(0, round((args.speed).bit_length() - 1))
    screen = Screen(layout, args.dry_run)
    rows = [int(r) for r in args.rows.split(",") if r.strip()]

    print(f"{len(rows)} replay(s) to play at {args.speed}x "
          f"({speed_clicks} speed-up clicks), recording at {args.hz:g} Hz")
    print("abort by moving the pointer to the top-left corner\n")

    done = 0
    try:
        for row in rows:
            if row >= len(replays):
                print(f"row {row}: no such replay")
                continue
            replay = replays[row]
            print(f"row {row}: {describe(replay, args.wtresults)}")
            label = replay.stem.strip("#").replace(" ", "-").replace(".", "")
            if play_one(screen, api, row, speed_clicks, label, args.hz, args.out):
                done += 1
            if not args.dry_run:
                time.sleep(layout["timings"]["return_to_hangar_s"])
                if not wait_for(api, False, 60, "the hangar"):
                    print("still in a battle; stopping rather than clicking blind")
                    break
    except KeyboardInterrupt:
        print("\naborted")
    finally:
        api.close()

    print(f"\n{done} of {len(rows)} replays played")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
