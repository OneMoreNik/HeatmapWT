"""Download map images and sizes from wt-tools.app.

The site publishes a map image per mode plus the size of the area it covers,
which is what the heatmap needs as a background. It does not say where that
square sits in world space, but the game does: a mission's battle area centre
(read offline by `bin/wtlevel.exe`) is the centre of the same square, so centre
plus size gives the world bounds of the image.

Verified on Berlin: the client reports a 1300 m grid with its corner at
(1730.32, 1717.07), so the centre is (2380.32, 1067.07); the mission blk puts
`dom_battle_area_hardcore` at (2380.3, 1067.1). wt-tools independently lists
Berlin as 1300 x 1300 m.

    python collector/fetch_maps.py --list
    python collector/fetch_maps.py berlin
    python collector/fetch_maps.py berlin --mode domination-1 --mode conquest-2
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import struct
import urllib.error
import urllib.request
from pathlib import Path

MANIFEST_URL = "https://wt-tools.app/manifest.json"
ASSET_BASE = "https://storage.googleapis.com/wt-map-files/maps"
USER_AGENT = "HeatmapWT/0.1 (personal map-learning tool)"


def fetch(url: str, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body


def load_manifest() -> dict:
    return json.loads(fetch(MANIFEST_URL).decode("utf-8"))


# The game's internal map name and wt-tools' key often differ: the game calls
# it "hurtgen" where wt-tools says "battle_of_hurtgen_forest", "vietnam_hills"
# against "vietnam", "eastern_europe_02" against "eastern_europe". These are
# resolved by shape where possible, and listed here where they cannot be.
MAP_ALIASES = {
    "hurtgen": "battle_of_hurtgen_forest",
    "soviet_range": "test-site_2271",
    "normandy_fields": "fields_of_normandy",
    "poland_fields": "fields_of_poland",
    "volokolamsk_surroundings": "surroundings_of_volokolamsk",
    "el_alamein": "second_battle_of_el_alamein",
    "rhine": "advance_to_the_rhine",
    "container_port": "cargo_port",
}


def resolve_map_key(manifest: dict, name: str) -> str | None:
    """wt-tools' key for a map the game calls `name`, or None.

    Tries, in order: the name itself, a known alias, the name with trailing
    numbering removed, progressively shorter prefixes, and finally any key
    whose words are a subset of the name's. wt-tools publishes 62 maps and the
    game has more, so None is a normal answer, not a failure.
    """
    if name in manifest:
        return name
    alias = MAP_ALIASES.get(name)
    if alias in manifest:
        return alias

    words = name.split("_")
    # "eastern_europe_02" -> "eastern_europe"
    while words and words[-1].isdigit():
        words.pop()
    for cut in range(len(words), 0, -1):
        candidate = "_".join(words[:cut])
        if candidate in manifest:
            return candidate

    # "battle_of_hurtgen_forest" contains "hurtgen"; prefer the longest match
    # so "poland" does not win over "fields_of_poland" for "poland_fields".
    wordset = set(words)
    matches = [k for k in manifest if wordset & set(k.split("_"))]
    if matches:
        return max(matches, key=lambda k: len(wordset & set(k.split("_"))))
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG header, without a decoder."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def fetch_mode(map_key: str, mode_key: str, mode: dict, out_root: Path) -> dict | None:
    out_dir = out_root / map_key / mode_key
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{ASSET_BASE}/{map_key}/{mode_key}"

    image_name = mode.get("image") or "map.png"
    image_path = out_dir / image_name
    if image_path.exists() and image_path.stat().st_size > 0:
        image = image_path.read_bytes()
        action = "cached"
    else:
        try:
            image = fetch(f"{base}/{image_name}")
        except urllib.error.HTTPError as err:
            print(f"  {map_key}/{mode_key}: image unavailable ({err.code})")
            return None
        image_path.write_bytes(image)
        action = "downloaded"

    size = png_size(image)
    meta = {
        "map": map_key,
        "mode": mode_key,
        "image": image_name,
        "size_m": mode.get("size"),
        "tile_m": mode.get("tile_size"),
        "image_px": list(size) if size else None,
        "source": f"{base}/{image_name}",
    }
    try:
        meta["wt_tools_meta"] = json.loads(fetch(f"{base}/meta.json").decode("utf-8"))
    except (urllib.error.HTTPError, json.JSONDecodeError):
        meta["wt_tools_meta"] = None
    (out_dir / "map.json").write_text(json.dumps(meta, indent=2))

    px = f"{size[0]}x{size[1]}px" if size else "unknown size"
    print(f"  {map_key}/{mode_key}: {action}, {len(image):,} B, {px}, "
          f"{mode.get('size')} m across")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("maps", nargs="*", help="map keys, e.g. berlin")
    parser.add_argument("--mode", action="append", default=[],
                        help="limit to these mode keys, e.g. domination-1")
    parser.add_argument("--out", type=Path, default=Path("data/maps"))
    parser.add_argument("--list", action="store_true", help="list maps and modes, download nothing")
    args = parser.parse_args()

    manifest = load_manifest()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    if args.list or not args.maps:
        print(f"{len(manifest)} maps in the manifest:")
        for map_key in sorted(manifest):
            modes = manifest[map_key]
            sizes = {m.get("size") for m in modes.values() if isinstance(m, dict)}
            size_text = ", ".join(str(s) for s in sorted(x for x in sizes if x))
            print(f"  {map_key:<34} {len(modes)} modes   {size_text} m")
        if not args.maps:
            print("\nname one or more maps to download, e.g. "
                  "python collector/fetch_maps.py berlin")
        return 0

    for map_key in args.maps:
        resolved = resolve_map_key(manifest, map_key)
        if resolved is None:
            print(f"{map_key}: wt-tools does not publish this map "
                  f"(it has {len(manifest)}); the pipeline will use the client's "
                  f"own grid instead of an image")
            continue
        if resolved != map_key:
            print(f"{map_key}: published as {resolved!r}")
        map_key = resolved
        modes = manifest[map_key]
        wanted = args.mode or list(modes)
        print(f"{map_key}: {len(wanted)} of {len(modes)} modes -> {args.out / map_key}")
        for mode_key in wanted:
            if mode_key not in modes:
                print(f"  {mode_key}: no such mode (have {', '.join(modes)})")
                continue
            fetch_mode(map_key, mode_key, modes[mode_key], args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
