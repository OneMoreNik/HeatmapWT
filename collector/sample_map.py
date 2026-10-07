"""Record unit positions from the game's local HTTP API while a replay plays.

The client exposes what it is currently showing on the map at
``localhost:8111``.  Playing a replay back and sampling that endpoint lets the
game decode the positions for us, which is the whole point: the replay packet
stream does not carry them as plain values.

``map_obj.json`` returns one entry per thing drawn on the map, with ``x`` and
``y`` as fractions of the map image.  ``map_info.json`` gives the world-space
corners, so the fractions become metres, and its ``map_generation`` counter
changes whenever a new map loads.  Each generation is recorded into its own
folder, so a session that spans several battles does not end up as one
unreadable file.

Objects carry no identity, so frames are recorded raw and tracks are stitched
afterwards.

    python collector/sample_map.py --hz 4
    python collector/sample_map.py --hz 4 --label berlin_dom

Recording starts when a map appears and keeps running across maps until
stopped, so it can be left alone through the menu, the replay, and back.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import time
from datetime import datetime, timezone
from pathlib import Path

# Not "localhost": that resolves to ::1 first on Windows, the game only listens
# on IPv4, and every request then pays the full connect timeout before falling
# back. That alone capped sampling at 0.5 Hz.
HOST = "127.0.0.1"
PORT = 8111

# How often to re-read map_info while recording, in seconds.
INFO_INTERVAL = 5.0

FIELDS = [
    "t", "frame", "idx", "type", "icon", "color",
    "x", "y", "dx", "dy", "sx", "sy", "ex", "ey",
    "world_x", "world_z", "blink",
]


class Api:
    """Keep-alive client for the game's local endpoints.

    A fresh connection per request caps the sample rate at about 0.5 Hz, which
    is too slow to follow a moving tank; reusing one connection removes that.
    """

    def __init__(self, timeout: float = 1.0):
        self.timeout = timeout
        self.conn: http.client.HTTPConnection | None = None

    def _connect(self) -> http.client.HTTPConnection:
        if self.conn is None:
            self.conn = http.client.HTTPConnection(HOST, PORT, timeout=self.timeout)
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None

    def get(self, path: str):
        """Decoded JSON from one endpoint, or None if unavailable."""
        for attempt in (0, 1):
            try:
                conn = self._connect()
                conn.request("GET", f"/{path}", headers={"Connection": "keep-alive"})
                body = conn.getresponse().read()
            except (OSError, http.client.HTTPException):
                self.close()
                if attempt == 0:
                    continue
                return None
            if not body.strip():
                return None
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return None
        return None


def map_is_live(info) -> bool:
    return isinstance(info, dict) and info.get("valid") is True


class WorldMapping:
    """Turns the API's 0..1 map fractions into world metres.

    ``map_min`` and ``map_max`` are the world-space corners of the map image.
    """

    def __init__(self, info: dict):
        self.min = info.get("map_min") or []
        self.max = info.get("map_max") or []
        self.usable = len(self.min) >= 2 and len(self.max) >= 2

    def to_world(self, x, y):
        """Map fraction -> world metres.

        The y fraction runs top-down while world Z runs bottom-up, so y is
        measured from map_max, not map_min. Verified against Berlin's three
        Domination capture points: the mission blk puts them at Z 1118.7,
        1140.5 and 1115.2, and this returns 1119, 1140 and 1115.
        """
        if not self.usable or x is None or y is None:
            return None, None
        return (round(self.min[0] + x * (self.max[0] - self.min[0]), 2),
                round(self.max[1] - y * (self.max[1] - self.min[1]), 2))


def row_for(obj: dict, mapping: WorldMapping, t: float, frame: int, idx: int) -> dict:
    world_x, world_z = mapping.to_world(obj.get("x"), obj.get("y"))
    return {
        "t": round(t, 3), "frame": frame, "idx": idx,
        "type": obj.get("type", ""), "icon": obj.get("icon", ""),
        "color": obj.get("color", ""),
        "x": obj.get("x"), "y": obj.get("y"),
        "dx": obj.get("dx"), "dy": obj.get("dy"),
        "sx": obj.get("sx"), "sy": obj.get("sy"),
        "ex": obj.get("ex"), "ey": obj.get("ey"),
        "world_x": world_x, "world_z": world_z,
        "blink": obj.get("blink"),
    }


class Recording:
    """One map's worth of samples, written as it goes."""

    def __init__(self, out_dir: Path, label: str, info: dict):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        generation = info.get("map_generation", "?")
        name = "-".join(p for p in (stamp, f"gen{generation}", label) if p)
        self.dir = out_dir / name
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "map_info.json").write_text(json.dumps(info, indent=2))

        self.mapping = WorldMapping(info)
        self.generation = generation
        self.path = self.dir / "map_obj.csv"
        self.handle = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.handle, fieldnames=FIELDS)
        self.writer.writeheader()
        self.started = time.monotonic()
        self.frame = 0
        self.rows = 0

        print(f"\nrecording map_generation {generation} -> {self.dir}")
        print(f"  map_min {self.mapping.min}  map_max {self.mapping.max}")
        if not self.mapping.usable:
            print("  no world corners in map_info; only map fractions recorded")

    def add(self, objects: list) -> int:
        now = time.monotonic() - self.started
        drawn = 0
        for idx, obj in enumerate(objects):
            if isinstance(obj, dict):
                self.writer.writerow(row_for(obj, self.mapping, now, self.frame, idx))
                self.rows += 1
                drawn += 1
        self.frame += 1
        return drawn

    def close(self) -> None:
        self.handle.close()
        span = time.monotonic() - self.started
        rate = self.frame / span if span > 0 else 0
        print(f"\n  {self.frame} frames, {self.rows} rows, {span:.0f}s "
              f"({rate:.1f} frames/s) -> {self.path}")
        if self.rows == 0:
            print("  nothing captured; removing")
            try:
                self.path.unlink()
                (self.dir / "map_info.json").unlink()
                self.dir.rmdir()
            except OSError:
                pass


def record(out_dir: Path, label: str, hz: float) -> None:
    period = 1.0 / hz
    api = Api()
    current: Recording | None = None
    print(f"polling localhost:{PORT} at {hz:g} Hz. Ctrl-C to stop.")
    print("waiting for a map...")

    info_checked = 0.0
    info = None
    try:
        while True:
            tick = time.monotonic()
            # Each request costs about 2s while the game is in the background,
            # so re-reading map_info every frame would halve the sample rate.
            # A battle change only needs catching within a few seconds.
            if tick - info_checked >= INFO_INTERVAL or info is None:
                info = api.get("map_info.json")
                info_checked = tick

            if not map_is_live(info):
                if current is not None:
                    current.close()
                    current = None
                    print("map gone; waiting for the next one...")
                info = None
                time.sleep(1.0)
                continue

            # A new battle bumps map_generation, so each one gets its own file.
            if current is None or info.get("map_generation", "?") != current.generation:
                if current is not None:
                    current.close()
                current = Recording(out_dir, label, info)

            objects = api.get("map_obj.json")
            if isinstance(objects, list) and objects:
                drawn = current.add(objects)
                if current.frame % max(1, int(hz) * 10) == 0:
                    current.handle.flush()
                    print(f"  {current.frame:5d} frames  {current.rows:7d} rows  "
                          f"{drawn:3d} objects in view", end="\r", flush=True)

            elapsed = time.monotonic() - tick
            if elapsed < period:
                time.sleep(period - elapsed)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if current is not None:
            current.close()
        api.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hz", type=float, default=10.0,
                        help="samples per second of wall clock; raise it when playing a replay back at speed")
    parser.add_argument("--out", type=Path, default=Path("data/live"))
    parser.add_argument("--label", default="", help="name to tag recordings with")
    args = parser.parse_args()
    record(args.out, args.label, args.hz)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
