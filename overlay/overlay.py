"""Show a map's heatmap on top of the game, on a hotkey.

Runs in the background and watches `map_info.json` from the client's own HTTP
endpoint. When a battle starts, that reports which square of the world the map
covers, which is enough to recognise the map and pick the heatmap built for it
with no manual choosing.

    Ctrl+Q   heatmap over the big pre-battle tactical map
    Ctrl+E   heatmap over the minimap
    Ctrl+R   next vehicle class
    Ctrl+D   hide

The window is click-through, so the game never notices it. Where the game draws
its maps cannot be read from anywhere, so the two rectangles are calibrated once
and remembered:

    python overlay/overlay.py --calibrate map
    python overlay/overlay.py --calibrate minimap

This reads only the public endpoint the game already serves to its own web map.
It does not touch the game's memory, files or input.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import sys
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from heatmapwt.png import read_png, write_png  # noqa: E402

# Not "localhost": on Windows that resolves to ::1 first, the game listens only
# on IPv4, and every request then pays the full connect timeout.
INFO_URL = "http://127.0.0.1:8111/map_info.json"
POLL_MS = 2000
KEY_MS = 40

CLASSES = ["all", "heavy", "medium", "light", "td", "spaa", "routes", "stops"]

# What the label says while a layer is up. The point of the label is to
# answer "what am I looking at", which the map name and battle count do not.
LABELS = {
    "all": "All vehicles",
    "heavy": "Heavy tanks",
    "medium": "Medium tanks",
    "light": "Light tanks",
    "td": "Tank destroyers",
    "spaa": "SPAA",
    "routes": "Routes - where they drive",
    "stops": "Stops - where they sit",
}

# Grid numbers come from the same source on both sides, so they should agree
# exactly; this only absorbs float noise.
FINGERPRINT_TOLERANCE_M = 1.0

# Below this the heatmap has nothing to say, and drawing it as near-black haze
# over the map only makes the map harder to read.
ALPHA_FLOOR = 24

# Tk makes exactly one colour transparent, so it has to be one the ramp never
# produces. The ramp starts at a deep blue and never reaches this.
MAGIC = (1, 2, 3)
MAGIC_HEX = "#010203"

DEFAULT_CONFIG = {
    "alpha": 0.45,
    "rects": {
        "map": [510, 90, 900, 900],
        "minimap": [1570, 700, 340, 340],
    },
    "hotkeys": {"map": "Q", "minimap": "E", "cycle": "R", "hide": "D"},
}

VK_CONTROL = 0x11
VK_SHIFT = 0x10


def config_path() -> Path:
    return Path(__file__).resolve().parent / "overlay_config.json"


def load_config() -> dict:
    path = config_path()
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        stored = json.loads(path.read_text(encoding="utf-8"))
        config.update({k: v for k, v in stored.items() if k != "rects"})
        config["rects"].update(stored.get("rects", {}))
    return config


def save_config(config: dict) -> None:
    config_path().write_text(json.dumps(config, indent=2), encoding="utf-8")


def map_info() -> dict | None:
    try:
        with urllib.request.urlopen(INFO_URL, timeout=1.0) as response:
            info = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    return info if info.get("valid") else None


def fingerprint(info: dict) -> list[float] | None:
    """Everything `map_info` says about the shape of the map, as one key."""
    parts = []
    for key in ("grid_zero", "grid_size", "grid_steps", "map_min", "map_max"):
        value = info.get(key) or []
        if len(value) < 2:
            return None
        parts += [round(float(value[0]), 2), round(float(value[1]), 2)]
    return parts


def matches(live: list[float], known: list[float]) -> bool:
    return (len(live) == len(known)
            and all(abs(a - b) <= FINGERPRINT_TOLERANCE_M
                    for a, b in zip(live, known)))


class Library:
    """The heatmaps on disk, indexed by the patch of world they cover."""

    def __init__(self, heatmaps: Path):
        self.heatmaps = heatmaps
        self.entries: list[dict] = []
        self.reload()

    def reload(self) -> None:
        self.entries = []
        # build_map.py writes one index per combined heatmap. process_recording
        # writes a per-battle sidecar of the same shape but no index: those are
        # for checking a capture worked, not for reading before a battle.
        for index_path in sorted(self.heatmaps.glob("*.index.json")):
            index = json.loads(index_path.read_text(encoding="utf-8"))
            prefix = index["prefix"]
            sidecar = self.heatmaps / f"{prefix}-all.json"
            counts = {}
            if sidecar.exists():
                counts = json.loads(sidecar.read_text(encoding="utf-8")).get("counts", {})
            self.entries.append({
                "prefix": prefix,
                "whole_map": bool(index.get("whole_map")),
                "fingerprints": index.get("fingerprints") or [],
                "battles": counts.get("battles", 0),
                "classes": [c for c in CLASSES
                            if (self.heatmaps / f"{prefix}-{c}.png").exists()],
            })

    def match(self, info: dict) -> dict | None:
        """The heatmap built for the map the client is showing, or None.

        Identified by what `map_info` reports, not by where the square happens
        to sit. Matching on the centre and size alone put Attica on Cargo Port's
        heatmap, because their battle areas are close enough together: a near
        miss is not a map, and drawing the wrong one confidently is worse than
        drawing nothing.
        """
        live = fingerprint(info)
        if live is None:
            return None
        hits = [entry for entry in self.entries
                if any(matches(live, known) for known in entry["fingerprints"])]
        if not hits:
            return None

        # Two layouts of one map can report the same grid -- Middle East does
        # for both of its -- and then nothing distinguishes them. The whole-map
        # combination holds both, so it is the honest answer.
        layouts = {entry["prefix"] for entry in hits if not entry["whole_map"]}
        if len(layouts) > 1:
            whole = [entry for entry in hits if entry["whole_map"]]
            if whole:
                return max(whole, key=lambda entry: entry["battles"])
        return min(hits, key=lambda entry: (entry["whole_map"], -entry["battles"]))


def fit(src_path: Path, width: int, height: int, cache: Path) -> Path:
    """`src_path` resampled to width x height, over the transparent colour.

    Tk can only scale by whole numbers, and the rectangle is whatever the game
    happens to use, so this does it properly and keeps the result: redrawing is
    instant after the first time at a given size.
    """
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{src_path.stem}-{width}x{height}.png"
    if out.exists() and out.stat().st_mtime >= src_path.stat().st_mtime:
        return out

    sw, sh, channels, pixels = read_png(src_path)
    rows = []
    for y in range(height):
        sy = (y + 0.5) * sh / height - 0.5
        y0 = min(max(int(math.floor(sy)), 0), sh - 1)
        y1 = min(y0 + 1, sh - 1)
        fy = min(max(sy - y0, 0.0), 1.0)
        row = bytearray(width * 4)
        for x in range(width):
            sx = (x + 0.5) * sw / width - 0.5
            x0 = min(max(int(math.floor(sx)), 0), sw - 1)
            x1 = min(x0 + 1, sw - 1)
            fx = min(max(sx - x0, 0.0), 1.0)
            a = (y0 * sw + x0) * channels
            b = (y0 * sw + x1) * channels
            c = (y1 * sw + x0) * channels
            d = (y1 * sw + x1) * channels
            out_px = [0, 0, 0, 0]
            for ch in range(4):
                top = pixels[a + ch] * (1 - fx) + pixels[b + ch] * fx
                bot = pixels[c + ch] * (1 - fx) + pixels[d + ch] * fx
                out_px[ch] = top * (1 - fy) + bot * fy
            # Hard cut rather than a blend towards the key colour: blending
            # would tint every faint cell near-black and fog the map.
            if out_px[3] < ALPHA_FLOOR:
                row[x * 4:x * 4 + 3] = bytes(MAGIC)
            else:
                row[x * 4:x * 4 + 3] = bytes(int(v) for v in out_px[:3])
            row[x * 4 + 3] = 255
        rows.append(bytes(row))
    write_png(out, width, height, rows)
    return out


class Overlay:
    def __init__(self, config: dict, heatmaps: Path, cache: Path):
        self.config = config
        self.library = Library(heatmaps)
        self.cache = cache
        self.entry: dict | None = None
        self.class_index = 0
        self.mode: str | None = None
        self.candidate: str | None = None
        self.image = None
        self.pressed: dict[tuple[str, bool], bool] = {}

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", config["alpha"])
        self.root.attributes("-transparentcolor", MAGIC_HEX)
        self.root.configure(bg=MAGIC_HEX)
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0, bg=MAGIC_HEX)
        self.canvas.pack(fill="both", expand=True)

        self.status = tk.Toplevel(self.root)
        self.status.overrideredirect(True)
        self.status.attributes("-topmost", True)
        self.status.attributes("-alpha", 0.85)
        self.status.configure(bg="#11151a")
        self.label = tk.Label(self.status, bg="#11151a", fg="#e6ecf2",
                              font=("Segoe UI", 10), padx=10, pady=5)
        self.label.pack()
        self.status.withdraw()

        self.root.after(50, self.make_click_through)
        self.root.after(POLL_MS, self.poll_game)
        self.root.after(KEY_MS, self.poll_keys)
        self.poll_game()

    # --- windows plumbing -------------------------------------------------

    def make_click_through(self) -> None:
        """Let clicks fall through to the game underneath."""
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x00080000, 0x00000020
        user32 = ctypes.windll.user32
        for window in (self.root, self.status):
            handle = user32.GetParent(window.winfo_id()) or window.winfo_id()
            style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
            user32.SetWindowLongW(handle, GWL_EXSTYLE,
                                  style | WS_EX_LAYERED | WS_EX_TRANSPARENT)

    def chord(self, key: str, shift: bool = False) -> bool:
        """True on the frame Ctrl+<key> goes down, with or without Shift.

        Polled rather than registered as a system hotkey so the key still
        reaches the game: nothing is swallowed, and nothing is injected.
        """
        user32 = ctypes.windll.user32
        held = (bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                and bool(user32.GetAsyncKeyState(ord(key)) & 0x8000))
        # Shift is part of the chord rather than ignored, or Ctrl+Shift+R would
        # fire Ctrl+R too and step forward and back in the same frame.
        down = held and bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000) == shift
        fired = down and not self.pressed.get((key, shift), False)
        self.pressed[(key, shift)] = down
        return fired

    # --- state ------------------------------------------------------------

    def poll_game(self) -> None:
        info = map_info()
        if info:
            if fingerprint(info) is not None:
                found = self.library.match(info)
                if found is None:
                    # Could be a map recorded since this started running.
                    self.library.reload()
                    found = self.library.match(info)
                name = found["prefix"] if found else None

                # Switch only on a reading seen twice. The client briefly
                # reports a different grid around a map change -- one Berlin
                # Conquest-2 capture starts with a 1700 m grid before settling
                # to the real 1300 m one -- and a single frame of that would
                # swap the map under you.
                if name == self.candidate and name != (
                        self.entry["prefix"] if self.entry else None):
                    # None twice over means a battle on a map with no heatmap.
                    # Leaving the previous one up would be worse than showing
                    # nothing: it is a different place, drawn as if it were this
                    # one.
                    self.entry = found
                    self.class_index = 0
                    if self.mode:
                        self.show(self.mode)
                self.candidate = name
        self.root.after(POLL_MS, self.poll_game)

    def poll_keys(self) -> None:
        keys = self.config["hotkeys"]
        if self.chord(keys["map"]):
            self.toggle("map")
        elif self.chord(keys["minimap"]):
            self.toggle("minimap")
        elif self.chord(keys["cycle"]):
            self.cycle(1)
        elif self.chord(keys["cycle"], shift=True):
            self.cycle(-1)
        elif self.chord(keys["hide"]):
            self.hide()
        self.root.after(KEY_MS, self.poll_keys)

    def current_class(self) -> str:
        if not self.entry or not self.entry["classes"]:
            return "all"
        return self.entry["classes"][self.class_index % len(self.entry["classes"])]

    def toggle(self, mode: str) -> None:
        if self.mode == mode:
            self.hide()
        else:
            self.show(mode)

    def cycle(self, step: int = 1) -> None:
        if self.entry and self.mode:
            self.class_index += step
            self.show(self.mode)

    def hide(self) -> None:
        self.mode = None
        self.root.withdraw()
        self.status.withdraw()

    def say(self, text: str, x: int, y: int) -> None:
        self.label.configure(text=text)
        self.status.geometry(f"+{x}+{max(0, y - 34)}")
        self.status.deiconify()
        self.status.lift()

    def show(self, mode: str) -> None:
        self.mode = mode
        x, y, width, height = self.config["rects"][mode]
        if self.entry is None:
            self.library.reload()
            self.root.withdraw()
            # Short, but not silent: a hotkey that does nothing at all reads as
            # the overlay being broken rather than the map being unrecorded.
            self.say("no data for this map", x, y)
            return

        vclass = self.current_class()
        source = self.library.heatmaps / f"{self.entry['prefix']}-{vclass}.png"
        if not source.exists():
            self.say(f"{LABELS.get(vclass, vclass)}: not built for this map", x, y)
            return

        scaled = fit(source, width, height, self.cache)
        self.image = tk.PhotoImage(file=str(scaled))
        self.canvas.delete("all")
        self.canvas.configure(width=width, height=height)
        self.canvas.create_image(0, 0, anchor="nw", image=self.image)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()

        self.say(LABELS.get(vclass, vclass), x, y)

    def run(self) -> None:
        """Run until Ctrl+C, which is how this is meant to be stopped.

        Tk raises the interrupt out of its own event loop, so without catching
        it here a perfectly normal "I am done playing" prints a traceback that
        reads like a crash.
        """
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        print("overlay stopped")


class Calibrator:
    """Drag a rectangle over the game's map once, and remember where it is."""

    def __init__(self, config: dict, mode: str, heatmaps: Path, cache: Path):
        self.config, self.mode = config, mode
        self.heatmaps, self.cache = heatmaps, cache
        x, y, width, height = config["rects"][mode]

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.75)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.configure(bg="#101820")
        self.canvas = tk.Canvas(self.root, highlightthickness=2,
                                highlightbackground="#4ad0ff", bg="#101820", bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.text = self.canvas.create_text(
            width // 2, height // 2, fill="#e6ecf2", width=width - 40,
            justify="center", font=("Segoe UI", 11), text="")
        self.grip = self.canvas.create_rectangle(0, 0, 0, 0, outline="#4ad0ff",
                                                 fill="#4ad0ff", width=0)
        self.refresh()

        self.drag = None
        self.canvas.bind("<Button-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.root.bind("<Key>", self.key)
        self.root.focus_force()

    def geometry(self) -> tuple[int, int, int, int]:
        self.root.update_idletasks()
        return (self.root.winfo_x(), self.root.winfo_y(),
                self.root.winfo_width(), self.root.winfo_height())

    def refresh(self) -> None:
        """Keep the size on screen: resizing blind is the slow way to do this."""
        x, y, width, height = self.geometry()
        self.canvas.coords(self.text, width // 2, height // 2)
        self.canvas.itemconfigure(
            self.text, width=max(120, width - 40),
            text=f"{self.mode}\n\n"
                 f"{width} x {height}  at  {x}, {y}\n\n"
                 f"Drag inside to move  ·  drag the corner to resize\n"
                 f"Arrows nudge  ·  Shift+arrows by 10  ·  Ctrl+Shift+arrows by 50\n\n"
                 f"Enter saves   Esc cancels")
        self.canvas.coords(self.grip, width - 26, height - 26, width - 2, height - 2)

    def press(self, event) -> None:
        _, _, width, height = self.geometry()
        corner = event.x > width - 28 and event.y > height - 28
        self.drag = ("resize" if corner else "move", event.x_root, event.y_root,
                     *self.geometry())

    def motion(self, event) -> None:
        if not self.drag:
            return
        kind, ox, oy, x, y, width, height = self.drag
        dx, dy = event.x_root - ox, event.y_root - oy
        if kind == "move":
            self.root.geometry(f"{width}x{height}+{x + dx}+{y + dy}")
        else:
            side = max(120, width + max(dx, dy))
            self.root.geometry(f"{side}x{side}+{x}+{y}")
        self.refresh()

    def key(self, event) -> None:
        x, y, width, height = self.geometry()
        shift = bool(event.state & 0x0001)
        control = bool(event.state & 0x0004)
        step = (50 if control else 10) if shift else (10 if control else 1)
        moves = {"Left": (-step, 0), "Right": (step, 0),
                 "Up": (0, -step), "Down": (0, step)}
        if event.keysym in moves:
            dx, dy = moves[event.keysym]
            if shift:
                side = max(120, width + dx + dy)
                self.root.geometry(f"{side}x{side}+{x}+{y}")
            else:
                self.root.geometry(f"{width}x{height}+{x + dx}+{y + dy}")
            self.refresh()
        elif event.keysym == "Return":
            self.config["rects"][self.mode] = list(self.geometry())
            save_config(self.config)
            print(f"{self.mode}: {self.config['rects'][self.mode]} saved to "
                  f"{config_path()}")
            self.root.destroy()
        elif event.keysym == "Escape":
            print("cancelled, nothing saved")
            self.root.destroy()

    def run(self) -> None:
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            print("cancelled, nothing saved")
            try:
                self.root.destroy()
            except tk.TclError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--heatmaps", type=Path, default=Path("data/heatmaps"))
    parser.add_argument("--cache", type=Path, default=Path("data/overlay-cache"))
    parser.add_argument("--calibrate", choices=["map", "minimap"],
                        help="position the rectangle for this view and save it")
    parser.add_argument("--alpha", type=float, help="0..1, how solid the heatmap is")
    args = parser.parse_args()

    config = load_config()
    if args.alpha is not None:
        config["alpha"] = args.alpha
        save_config(config)

    if args.calibrate:
        Calibrator(config, args.calibrate, args.heatmaps, args.cache).run()
        return 0

    library = Library(args.heatmaps)
    if not library.entries:
        print(f"no combined heatmaps in {args.heatmaps}. Run "
              f"python tools/build_map.py first.")
        return 1
    keys = config["hotkeys"]
    print(f"{len(library.entries)} maps ready. "
          f"Ctrl+{keys['map']} tactical map, Ctrl+{keys['minimap']} minimap, "
          f"Ctrl+{keys['cycle']} next class, Ctrl+{keys['hide']} hide. "
          f"Ctrl+C here to stop.")
    Overlay(config, args.heatmaps, args.cache).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
