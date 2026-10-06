"""Pull the vehicle roster out of a decompressed replay packet stream.

The entity-creation part of the stream carries each spawned unit's model
path as a plain string (``tankModels/germ_pzkpfw_VI_ausf_e_tiger``), so the
roster can be read without decoding the bit-packed state updates.
"""

from __future__ import annotations

import re

_MODEL_RE = re.compile(rb"(?:tank|air|ship)Models/([A-Za-z0-9_]{4,48})")
_NATIONS = ("germ", "ussr", "us", "uk", "jp", "cn", "it", "fr", "sw", "il")

# Props and scenery share the model namespace with playable vehicles.
_NOT_VEHICLES = re.compile(
    r"^(?:.*_(?:tram|wagon|clone)(?:_[a-z0-9]+)?|.*_flak\d+|.*_sdkfz_9_.*)$"
)


def _nation(model: str) -> str | None:
    """Nation prefix of a model id, or ``None`` if it has no usable name.

    Strings can be cut short at a compression-frame boundary, which leaves a
    bare prefix like ``germ_``; those are dropped.
    """
    for nation in _NATIONS:
        if model.startswith(f"{nation}_") and len(model) > len(nation) + 2:
            return nation
    return None


def vehicle_models(body: bytes) -> list[str]:
    """Unique playable-vehicle model ids, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _MODEL_RE.finditer(body):
        model = match.group(1).decode("ascii")
        if _nation(model) is None or _NOT_VEHICLES.match(model):
            continue
        seen.setdefault(model, None)
    return list(seen)


def by_nation(models: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for model in models:
        grouped.setdefault(_nation(model) or "?", []).append(model)
    return grouped
