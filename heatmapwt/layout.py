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

import math
import re

# Matched against the normalised name, best first.
BATTLE_AREA = ("battlearea",)
CAPTURE_AREA = ("capturearea", "capturezone")
# "resp01" is Middle East's spelling of a respawn point; without it that
# mission yields no geometry at all, since it defines no battle area either.
SPAWN_AREA = ("tankspawn", "killarea", "spawn", "resp")

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


def pick_areas(layout: dict, kind: tuple[str, ...],
               near: tuple[float, float] | None = None) -> list[dict]:
    """Areas of one kind, from one difficulty variant.

    Areas whose name starts with ``briefing_`` are the ones drawn on the
    pre-battle briefing. They are preferred against only when real ones exist,
    because some missions define nothing else.

    Which variant is right cannot be read off the name. Berlin's client grid
    matches `dom_battle_area_hardcore` exactly; Volokolamsk's matches the plain
    `briefing_battlearea`, and its `_hardcore` one describes a square 1152 m
    away, which on a 1400 m map is a different part of the world. So when the
    caller knows where the battle actually was -- the client reports it in every
    recording -- the variant nearest that point wins, and the name is only
    consulted as a last resort.
    """
    areas = layout.get("captures", [])
    matched = [a for a in areas
               if any(k in normalise(a.get("name", "")) for k in kind)]
    if not matched:
        return []

    if near is not None:
        # Proximity settles the briefing split too, not just the difficulty.
        # Hurtgen's only non-briefing "spawn" areas are `teamB_artillery_spawn_*`,
        # 3.3 km outside the battle area, so preferring non-briefing areas on
        # principle picked artillery positions over the tank spawns and put every
        # track start a median 3863 m from a "spawn".
        groups: dict[tuple[bool, int], list[dict]] = {}
        for area in matched:
            key = (area.get("name", "").startswith("briefing_"),
                   _difficulty_rank(area["name"]))
            groups.setdefault(key, []).append(area)

        def distance(group: list[dict]) -> float:
            cx = sum(a["x"] for a in group) / len(group)
            cz = sum(a["z"] for a in group) / len(group)
            return math.hypot(cx - near[0], cz - near[1])
        return groups[min(groups, key=lambda key: distance(groups[key]))]

    real = [a for a in matched if not a.get("name", "").startswith("briefing_")]
    pool = real or matched
    ranks: dict[int, list[dict]] = {}
    for area in pool:
        ranks.setdefault(_difficulty_rank(area["name"]), []).append(area)
    return ranks[min(ranks)]


def battle_area_centre(layout: dict,
                       near: tuple[float, float] | None = None) -> tuple[float, float] | None:
    """Centre of the playable square, which is also the map image's centre.

    Verified on Berlin: the client reports a 1300 m grid whose centre is
    (2380.32, 1067.07), and the mission puts its battle area at
    (2380.3, 1067.1).
    """
    areas = pick_areas(layout, BATTLE_AREA, near)
    if areas:
        return areas[0]["x"], areas[0]["z"]
    # No battle area: fall back to the midpoint of the spawns, which bound the
    # playable space well enough to place a map image.
    spawns = pick_areas(layout, SPAWN_AREA, near)
    if len(spawns) >= 2:
        return (sum(s["x"] for s in spawns) / len(spawns),
                sum(s["z"] for s in spawns) / len(spawns))
    return None


def capture_points(layout: dict,
                   near: tuple[float, float] | None = None) -> list[dict]:
    return pick_areas(layout, CAPTURE_AREA, near)


def spawns(layout: dict, near: tuple[float, float] | None = None) -> list[dict]:
    return pick_areas(layout, SPAWN_AREA, near)
