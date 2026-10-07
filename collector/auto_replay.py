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
import tempfile
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

# The window title carries the renderer ("War Thunder (DirectX 12, 64bit)"),
# so match the window class instead, which is stable across renderers.
WINDOW_CLASS = "DagorWClass"
WINDOW_TITLE_HINT = "War Thunder"
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_ESCAPE = 0x1B


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


class Screen:
    """Synthetic mouse input, with an abort corner and a dry-run mode."""

    def __init__(self, layout: dict, dry_run: bool, shots_dir: Path | None = None):
        self.layout = layout
        self.dry_run = dry_run
        self.shots_dir = shots_dir
        self.shot_index = 0
        if shots_dir is not None:
            shots_dir.mkdir(parents=True, exist_ok=True)
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

    def _move(self, x: int, y: int) -> None:
        ax = int(x * 65535 / max(self.width - 1, 1))
        ay = int(y * 65535 / max(self.height - 1, 1))
        event = INPUT(type=INPUT_MOUSE,
                      mi=MOUSEINPUT(ax, ay, 0,
                                    MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, None))
        user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

    def region_png(self, region: dict) -> bytes | None:
        """PNG bytes of a screen region, for before/after comparison.

        Used to tell whether a click actually did anything: the speed label
        is either identical to before, meaning the button was missed, or it
        is not. Cheaper and far more robust than reading the text.
        """
        if self.dry_run:
            return None
        x, y = self.to_screen(region)
        w = int(region.get("w", 60) * self.scale_x)
        h = int(region.get("h", 20) * self.scale_y)
        out = Path(tempfile.gettempdir()) / f"wt-region-{x}-{y}.png"
        script = Path(__file__).with_name("crop.ps1")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-X", str(x - w // 2), "-Y", str(y - h // 2), "-W", str(w), "-H", str(h),
             "-Out", str(out)],
            capture_output=True, timeout=60)
        if result.returncode != 0 or not out.exists():
            return None
        return out.read_bytes()

    def shot(self, tag: str) -> None:
        """Capture the screen, so a misfire can be seen rather than guessed at."""
        if self.shots_dir is None:
            return
        self.shot_index += 1
        out = self.shots_dir / f"{self.shot_index:03d}-{tag}.png"
        script = Path(__file__).with_name("screenshot.ps1")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(script), "-Out", str(out)],
                       capture_output=True, timeout=60)

    def press(self, vk: int, label: str) -> None:
        """Tap a key, used to back out of the post-battle screen."""
        if self.dry_run:
            print(f"    [dry-run] press {label}")
            return
        if not game_is_focused() and not focus_game():
            raise RuntimeError("the game is not the foreground window; refusing to type")
        for flags in (0, KEYEVENTF_KEYUP):
            event = INPUT(type=INPUT_KEYBOARD,
                          ki=KEYBDINPUT(vk, 0, flags, 0, None))
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
            time.sleep(0.04)
        print(f"    press {label}", flush=True)
        time.sleep(self.layout["timings"]["after_click_s"])
        self.shot("press")

    def click(self, point: dict, label: str = "") -> None:
        x, y = self.to_screen(point)
        note = point.get("note", label)
        if self.dry_run:
            print(f"    [dry-run] click ({x},{y}) {note}")
            return
        if self.aborted():
            raise KeyboardInterrupt("pointer parked in the top-left corner")
        if not game_is_focused() and not focus_game():
            raise RuntimeError(
                "the game is not the foreground window; refusing to click, "
                "since the input would go to whatever is in front instead")
        # Repeated clicks on the same pixel get swallowed: four presses of the
        # speed button only advanced it one step. Moving away and back makes
        # each one a fresh hover, and the button then registers every time.
        self._move(self.width // 2, self.height - 4)
        time.sleep(0.05)
        self._move(x, y)
        time.sleep(0.12)
        for flags in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
            event = INPUT(type=INPUT_MOUSE,
                          mi=MOUSEINPUT(0, 0, 0, flags, 0, None))
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
            time.sleep(0.06)
        print(f"    click ({x},{y}) {note}", flush=True)
        time.sleep(self.layout["timings"]["after_click_s"])
        self.shot(f"click-{x}-{y}")


def find_game() -> int:
    """Handle of the game window, or 0."""
    handle = user32.FindWindowW(WINDOW_CLASS, None)
    if handle:
        return handle
    # Fall back to a title scan in case the class ever changes.
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.startswith(WINDOW_TITLE_HINT):
                found.append(hwnd)
        return True

    user32.EnumWindows(proc(visit), 0)
    return found[0] if found else 0


def focus_game(timeout: float = 6.0) -> bool:
    """Bring the game to the front and confirm it got there.

    Windows refuses SetForegroundWindow from a process that does not already
    own the foreground, and does so silently. Attaching to the current
    foreground window's input queue first lifts that restriction; the result
    is then verified rather than assumed, because a failed focus means the
    clicks land in whatever application is in front instead.
    """
    handle = find_game()
    if not handle:
        return False
    user32.ShowWindow(handle, 9)  # SW_RESTORE

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = user32.GetForegroundWindow()
        if current == handle:
            return True
        ours = user32.GetWindowThreadProcessId(current, None)
        theirs = user32.GetWindowThreadProcessId(handle, None)
        attached = ours and theirs and ours != theirs
        if attached:
            user32.AttachThreadInput(ours, theirs, True)
        user32.BringWindowToTop(handle)
        user32.SetForegroundWindow(handle)
        if attached:
            user32.AttachThreadInput(ours, theirs, False)
        time.sleep(0.4)
    return user32.GetForegroundWindow() == handle


def game_is_focused() -> bool:
    handle = find_game()
    return bool(handle) and user32.GetForegroundWindow() == handle


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
        # The author's team is the only side a client replay can follow, so
        # report its best player rather than the best overall.
        team = battle.get("authorTeam")
        mine = [p for p in battle.get("players", []) if p.get("team") == team]
        top = (mine or battle.get("players") or [{}])[0].get("name", "?")
        return (f"{replay.name}  {battle.get('battleType', '?')}  "
                f"{battle.get('timePlayed', 0):.0f}s  team {team} best: {top}")
    except Exception:
        return replay.name


def resolve_target(replay: Path, wtresults: Path, follow: str):
    """Who to spectate, and which row of the in-replay player list they are.

    The client's in-replay player list is its team's players in the order the
    results block stores them, so a row index can be computed offline. That
    order only settles once everyone has joined, which is why the click waits.

    `follow` is "best" (highest scorer on the author's team), "author", or a
    name to match case-insensitively.
    """
    if not wtresults.exists():
        return None
    result = subprocess.run([str(wtresults), "-json", str(replay)],
                            capture_output=True, text=True, timeout=60,
                            encoding="utf-8", errors="replace")
    try:
        battle = json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        return None

    players = battle.get("players", [])
    overall_best = players[0] if players else None
    if follow == "overall":
        target = overall_best
    elif follow == "author":
        wanted = [p for p in players if p["name"] in battle.get("author", "")]
        target = wanted[0] if wanted else None
    elif follow == "best":
        team = battle.get("authorTeam")
        same = [p for p in players if p["team"] == team]
        target = same[0] if same else (players[0] if players else None)
    else:
        wanted = [p for p in players if follow.lower() in p["name"].lower()]
        target = wanted[0] if wanted else None
    if target is None:
        return None

    # Row index is the player's place among their own team, in slot order.
    side = sorted((p for p in players if p["team"] == target["team"]),
                  key=lambda p: p["slot"])
    try:
        row = side.index(target)
    except ValueError:
        return None
    return {"name": target["name"], "score": target["score"],
            "team": target["team"], "row": row,
            "is_author": target["name"] in battle.get("author", ""),
            # The opposing team is listed down the right edge, not the left.
            "is_enemy": target["team"] != battle.get("authorTeam"),
            "overall_best": overall_best["name"] if overall_best else None}


def write_source(out_dir: Path, label: str, replay: Path, speed: int,
                 wtresults: Path, prefix: str) -> None:
    """Record which replay a capture came from, for tools/process_recording.py.

    Without this the analysis cannot tell which map a folder of map positions
    belongs to, and the map image and layout have to be named by hand.
    """
    source = {"replay": str(replay), "label": label, "speed": speed, "prefix": prefix}
    if wtresults.exists():
        result = subprocess.run([str(wtresults), "-json", str(replay)],
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
            pass
    # The recorder names its own folder, so write into whichever folders it
    # created for this replay since the run began.
    for folder in sorted(out_dir.glob(f"*{label}*")):
        if folder.is_dir():
            (folder / "source.json").write_text(json.dumps(source, indent=2),
                                                encoding="utf-8")


def wait_for(api: Api, want_live: bool, timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if map_is_live(api.get("map_info.json")) == want_live:
            return True
        time.sleep(1.0)
    print(f"    timed out waiting for {what}")
    return False


def play_one(screen: Screen, api: Api, row: int, speed_clicks: int,
             label: str, record_hz: float, out_dir: Path,
             open_menu: bool = True, replay: Path | None = None,
             speed: int = 1, wtresults: Path | None = None,
             target: dict | None = None) -> bool:
    layout = screen.layout
    timings = layout["timings"]
    hangar, dialog, view = layout["hangar"], layout["replay_dialog"], layout["replay_view"]

    listing = dialog["list"]
    if row >= listing["visible_rows"]:
        print(f"    row {row} is past the {listing['visible_rows']} visible rows; "
              f"scrolling is not implemented")
        return False

    # A finished replay drops back into the Replays dialog, so it only needs
    # opening for the first one of a run.
    if open_menu:
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
        if speed_clicks:
            print("    replay running; setting playback speed")
            before = screen.region_png(view["speed_label"])
            for _ in range(speed_clicks):
                screen.click(view["speed_up"])
            after = screen.region_png(view["speed_label"])
            if before is not None and before == after:
                print("    WARNING: the speed label did not change, so the >> button was "
                      "missed; continuing at 1x. Re-measure replay_view.speed_up.")
        else:
            print("    replay running at 1x")

        if target is not None and not target["is_author"]:
            # The in-replay list is that team's players in results-block order,
            # but players who have not joined yet are simply absent, which
            # shifts everyone above them up. Waiting past the join phase is
            # what makes the row index correct; measured in battle seconds, so
            # a fast playback waits proportionally less wall clock.
            battle_wait = timings["player_list_settle_battle_s"]
            wall_wait = min(60.0, max(4.0, battle_wait / max(speed, 1)))
            print(f"    waiting {wall_wait:.0f}s for the roster to fill "
                  f"({battle_wait:.0f}s of battle time)")
            time.sleep(wall_wait)
            plist = view["enemy_player_list"] if target.get("is_enemy") else view["player_list"]
            side = "enemy" if target.get("is_enemy") else "own"
            print(f"    following {target['name']} ({target['score']} points), "
                  f"{side}-side row {target['row']}")
            screen.click({"x": plist["x"],
                          "y": plist["first_y"] + target["row"] * plist["row_height"],
                          "note": f"player row {target['row']}: {target['name']}"})

        start = time.monotonic()
        while map_is_live(api.get("map_info.json")):
            if screen.aborted():
                raise KeyboardInterrupt("pointer parked in the top-left corner")
            time.sleep(2.0)
        print(f"    battle ended after {time.monotonic() - start:.0f}s of playback")
        # Esc is deliberately not pressed: inside a replay it switches the
        # spectated player rather than exiting, and a finished replay returns
        # to the Replays dialog on its own.
        time.sleep(timings["battle_end_grace_s"])
    finally:
        recorder.terminate()
        try:
            recorder.wait(timeout=20)
        except subprocess.TimeoutExpired:
            recorder.kill()
        if replay is not None and wtresults is not None:
            write_source(out_dir, label, replay, speed, wtresults, label)
    return True


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, default=Path("D:/Games/WarThunder/Replays"))
    parser.add_argument("--layout", type=Path, default=here / "ui_layout.json")
    parser.add_argument("--wtresults", type=Path, default=Path("bin/wtresults.exe"))
    parser.add_argument("--out", type=Path, default=Path("data/live"))
    parser.add_argument("--rows", default="", help="comma-separated row numbers, newest first")
    parser.add_argument("--replay", action="append", default=[],
                        help="select by filename instead of row number, matched as a "
                             "substring. Row numbers shift every time a battle is "
                             "played, so this is the safer way to name one")
    parser.add_argument("--speed", type=int, default=1,
                        help="playback speed, reached by doubling from 1x. The default "
                             "of 1 skips the speed button entirely: nobody is waiting on "
                             "an unattended run, and 1x gives far denser samples")
    parser.add_argument("--hz", type=float, default=20.0, help="recording rate")
    parser.add_argument("--list", action="store_true", help="list replays with their row numbers")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every click without sending any input")
    parser.add_argument("--follow", default="author",
                        help="who to spectate: author (the default, which needs no "
                             "clicking), best for the highest scorer on the author's "
                             "team, overall for the highest scorer in the battle even if "
                             "that is an enemy, or part of a player name")
    parser.add_argument("--shots", type=Path,
                        help="save a screenshot after every click into this directory")
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
        raise SystemExit(f"no window of class {WINDOW_CLASS!r}; is the game running?")

    speed_clicks = max(0, round((args.speed).bit_length() - 1))
    screen = Screen(layout, args.dry_run, args.shots)
    rows = [int(r) for r in args.rows.split(",") if r.strip()]
    for wanted in args.replay:
        matched = [i for i, r in enumerate(replays) if wanted.lower() in r.name.lower()]
        if not matched:
            print(f"no replay matches {wanted!r}")
        elif len(matched) > 1:
            print(f"{wanted!r} matches {len(matched)} replays; be more specific:")
            for i in matched:
                print(f"  row {i}: {replays[i].name}")
        else:
            rows.append(matched[0])
    if not rows:
        raise SystemExit("nothing selected; use --rows or --replay, or --list to look")
    # Keep the client's own order so the list is walked top to bottom.
    rows = sorted(set(rows))

    print(f"{len(rows)} replay(s) to play at {args.speed}x "
          f"({speed_clicks} speed-up clicks), recording at {args.hz:g} Hz")
    print("abort by moving the pointer to the top-left corner\n")

    done = 0
    need_menu = True
    try:
        for row in rows:
            if row >= len(replays):
                print(f"row {row}: no such replay")
                continue
            replay = replays[row]
            print(f"row {row}: {describe(replay, args.wtresults)}")
            target = resolve_target(replay, args.wtresults, args.follow)
            if args.follow != "author" and target is None:
                print(f"    no player matches {args.follow!r}; watching the author instead")
            label = replay.stem.strip("#").replace(" ", "-").replace(".", "")
            if play_one(screen, api, row, speed_clicks, label, args.hz, args.out,
                        open_menu=need_menu, replay=replay, speed=args.speed,
                        wtresults=args.wtresults, target=target):
                done += 1
                need_menu = False
            else:
                # Something went wrong, so do not assume where the UI is.
                need_menu = True
            if not args.dry_run and not wait_for(api, False, 60, "the replay list"):
                print("still in a replay; stopping rather than clicking blind")
                break
    except KeyboardInterrupt:
        print("\naborted")
    finally:
        api.close()

    print(f"\n{done} of {len(rows)} replays played")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
