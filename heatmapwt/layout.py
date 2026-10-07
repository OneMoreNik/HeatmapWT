"""Find the capture points, spawns and playable square in a mission layout.

Missions do not name their areas consistently. Berlin has
`dom_capture_area_01_hardcore` and `dom_battle_area_hardcore`; Tunisia only has
`_arcade` variants; Poland writes `battlearea` as one word, uses `captureZone`
and `tankSpawn` in camel case, marks Realistic with `Rb` rather than
`hardcore`, and prefixes *every* area with `briefing_`, so excluding those
leaves nothing at all.

So names are normalised to lowercase letters and digits before matching, every
spelling seen is accepted, and the difficulty variant is chosen by preference
with a fall back rather than being assumed.
"""

from __future__ import annotations

import re

# Matched against the normalised name, best first.
BATTLE_AREA = ("battlearea",)
CAPTURE_AREA = ("capturearea", "capturezone")
SPAWN_AREA = ("tankspawn", "killarea", "spawn")

# Realistic Battles first, then anything, so a map that only defines Arcade
# areas still yields geometry.
DIFFICULTIES = ("hardcore", "realistic", "rb", "arcade", "")


def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _difficulty_rank(name: str) -> int:
    flat = normalise(name)
    for i, suffix in enumerate(DIFFICULTIES):
        if suffix and suffix in flat:
            return i
    return len(DIFFICULTIES) - 1


def pick_areas(layout: dict, kind: tuple[str, ...]) -> list[dict]:
    """Areas of one kind, from the best difficulty variant available.

    Areas whose name starts with ``briefing_`` are the ones drawn on the
    pre-battle briefing. They are preferred against only when real ones exist,
    because some missions define nothing else.
    """
    areas = layout.get("captures", [])
    matched = [a for a in areas
               if any(k in normalise(a.get("name", "")) for k in kind)]
    if not matched:
        return []

    real = [a for a in matched if not a.get("name", "").startswith("briefing_")]
    pool = real or matched

    best = min(_difficulty_rank(a["name"]) for a in pool)
    return [a for a in pool if _difficulty_rank(a["name"]) == best]


def battle_area_centre(layout: dict) -> tuple[float, float] | None:
    """Centre of the playable square, which is also the map image's centre.

    Verified on Berlin: the client reports a 1300 m grid whose centre is
    (2380.32, 1067.07), and the mission puts its battle area at
    (2380.3, 1067.1).
    """
    areas = pick_areas(layout, BATTLE_AREA)
    if areas:
        return areas[0]["x"], areas[0]["z"]
    # No battle area: fall back to the midpoint of the spawns, which bound the
    # playable space well enough to place a map image.
    spawns = pick_areas(layout, SPAWN_AREA)
    if len(spawns) >= 2:
        return (sum(s["x"] for s in spawns) / len(spawns),
                sum(s["z"] for s in spawns) / len(spawns))
    return None


def capture_points(layout: dict) -> list[dict]:
    return pick_areas(layout, CAPTURE_AREA)


def spawns(layout: dict) -> list[dict]:
    return pick_areas(layout, SPAWN_AREA)
