"""Reader for War Thunder ``.wrpl`` replay files.

A ``.wrpl`` is laid out as:

===========================  ================================================
region                       contents
===========================  ================================================
``0x000`` .. ``0x4c4``       fixed-size header (map, mission, session id, ...)
``0x4c4`` .. body start      ``mission_settings`` in binary BLK form
body start .. ``rezOffset``  the replay packet stream, zstd compressed
``rezOffset`` .. EOF         battle results in binary BLK form
===========================  ================================================

Only the header fields verified against real replays are exposed.  Offsets
were confirmed by cross-checking two Berlin Ground RB replays recorded on
2026-10-06 (``level``, ``mission``, ``battle_type``, ``session_id`` and
``start_time`` all match the files they came from).
"""

from __future__ import annotations

import struct
from compression import zstd
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAGIC = 0x1000ACE5  # bytes e5 ac 00 10, read little-endian

# Verified header offsets.
_OFF_MAGIC = 0x000
_OFF_VERSION = 0x004
_OFF_LEVEL = 0x008          # char[128]  "levels/avg_berlin.bin"
_OFF_MISSION = 0x088        # char[260]  "gamedata/missions/.../berlin_dom.blk"
_OFF_BATTLE_TYPE = 0x18C    # char[128]  "berlin_Dom"
_OFF_ENVIRONMENT = 0x20C    # char[128]  "day"
_OFF_VISIBILITY = 0x28C     # char[32]   "rain"
_OFF_REZ_OFFSET = 0x2AC     # uint32     start of the results BLK
_OFF_SESSION_ID = 0x2DC     # uint64     id used to fetch the server replay
_OFF_PART_NUMBER = 0x2E4    # uint8      which 30s part, for server replays
_OFF_IS_SERVER = 0x2E6      # uint8      0x5a in a server replay, 0 in a client one
_OFF_SETTINGS_SIZE = 0x2EC  # uint16     length of the mission_settings BLK
_OFF_LOC_NAME = 0x30C       # char[128]  "missions/_Dom;berlin/name"
_OFF_START_TIME = 0x38C     # uint32     unix time the battle started

HEADER_SIZE = 1234          # 0x4D2; the mission_settings BLK follows it


_BODY_MARKER = b"\x00\x22\x06\x00"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

SERVER_REPLAY_URL = "https://wt-game-replays.warthunder.com/{session_id:x}/{part:04d}.wrpl"


def _cstr(buf: bytes, offset: int, size: int) -> str:
    raw = buf[offset : offset + size]
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


@dataclass(frozen=True)
class ReplayHeader:
    """The fields of a replay header we can read with confidence."""

    version: int
    level: str
    mission: str
    battle_type: str
    environment: str
    visibility: str
    rez_offset: int
    session_id: int
    part_number: int
    is_server: bool
    settings_size: int
    loc_name: str
    start_time: datetime

    @property
    def map_name(self) -> str:
        """``levels/avg_berlin.bin`` -> ``berlin``."""
        stem = self.level.rsplit("/", 1)[-1].removesuffix(".bin")
        return stem.removeprefix("avg_")

    @property
    def layout(self) -> str:
        """``gamedata/.../berlin_conq2.blk`` -> ``conq2``.

        Map plus layout is the key heatmaps are grouped by: the same map
        with different capture points produces different movement.
        """
        stem = self.mission.rsplit("/", 1)[-1].removesuffix(".blk")
        prefix = f"{self.map_name}_"
        return stem.removeprefix(prefix) if stem.startswith(prefix) else stem

    @property
    def session_hex(self) -> str:
        """Session id as it appears in the server-replay URL (16 hex digits)."""
        return f"{self.session_id:016x}"

    @property
    def packets_offset(self) -> int:
        """Where the compressed packet stream starts."""
        return HEADER_SIZE + self.settings_size

    def server_replay_url(self, part: int = 0) -> str:
        return SERVER_REPLAY_URL.format(session_id=self.session_id, part=part)


def read_header(path: str | Path) -> ReplayHeader:
    with open(path, "rb") as fh:
        buf = fh.read(HEADER_SIZE)
    if len(buf) < HEADER_SIZE:
        raise ValueError(f"{path}: too short to be a replay")
    magic = struct.unpack_from("<I", buf, _OFF_MAGIC)[0]
    if magic != MAGIC:
        raise ValueError(f"{path}: bad magic {magic:#010x}, expected {MAGIC:#010x}")
    ts = struct.unpack_from("<I", buf, _OFF_START_TIME)[0]
    return ReplayHeader(
        version=struct.unpack_from("<I", buf, _OFF_VERSION)[0],
        level=_cstr(buf, _OFF_LEVEL, 128),
        mission=_cstr(buf, _OFF_MISSION, 260),
        battle_type=_cstr(buf, _OFF_BATTLE_TYPE, 128),
        environment=_cstr(buf, _OFF_ENVIRONMENT, 128),
        visibility=_cstr(buf, _OFF_VISIBILITY, 32),
        rez_offset=struct.unpack_from("<I", buf, _OFF_REZ_OFFSET)[0],
        session_id=struct.unpack_from("<Q", buf, _OFF_SESSION_ID)[0],
        part_number=buf[_OFF_PART_NUMBER],
        is_server=buf[_OFF_IS_SERVER] == 0x5A,
        settings_size=struct.unpack_from("<H", buf, _OFF_SETTINGS_SIZE)[0],
        loc_name=_cstr(buf, _OFF_LOC_NAME, 128),
        start_time=datetime.fromtimestamp(ts, tz=timezone.utc),
    )


def _body_offset(raw: bytes, header: ReplayHeader) -> int:
    """Offset of the first compressed frame of the packet stream."""
    offset = header.packets_offset
    if raw[offset : offset + 4] == _ZSTD_MAGIC:
        return offset
    found = raw.find(_ZSTD_MAGIC, HEADER_SIZE)
    if found < 0:
        raise ValueError("no zstd packet stream found")
    return found


def read_body(path: str | Path) -> bytes:
    """Decompress the replay packet stream.

    The region between the mission settings and ``rezOffset`` is a run of
    concatenated zstd frames; ``decompress`` walks all of them.
    """
    raw = Path(path).read_bytes()
    header = read_header(path)
    start = _body_offset(raw, header)
    end = header.rez_offset or len(raw)
    return zstd.ZstdDecompressor().decompress(raw[start:end])


def read_results_blk(path: str | Path) -> bytes:
    """The raw battle-results BLK blob at the end of the file (undecoded)."""
    raw = Path(path).read_bytes()
    header = read_header(path)
    return raw[header.rez_offset :] if header.rez_offset else b""
