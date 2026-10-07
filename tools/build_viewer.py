"""Build a self-contained HTML viewer for a map's heatmaps.

Lays each heatmap over the map image with a class switcher and an opacity
slider, which is the pre-battle overlay from the brief in a form that can be
checked before any of it goes near the game. Everything is inlined as data
URIs so the file can be opened straight from disk or sent on.

    python tools/build_viewer.py data/maps/berlin/conquest-2 \
        --heatmaps data/heatmaps --prefix conq2 --out data/heatmaps/berlin-conq2.html
"""

from __future__ import annotations

import argparse
import base64
import html
import json
from pathlib import Path

# Order matters: this is the order the buttons appear in.
LAYERS = [
    ("all", "All"),
    ("heavy", "Heavy"),
    ("medium", "Medium"),
    ("light", "Light"),
    ("td", "TD"),
    ("spaa", "SPAA"),
    ("routes", "Routes"),
    ("stops", "Stops"),
]


def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build(map_dir: Path, heatmaps: Path, prefix: str) -> str:
    map_meta = json.loads((map_dir / "map.json").read_text())
    map_image = data_uri(map_dir / map_meta["image"])

    layers = []
    for key, label in LAYERS:
        png = heatmaps / f"{prefix}-{key}.png"
        if not png.exists():
            continue
        meta_path = png.with_suffix(".json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        counts = meta.get("counts", {})
        layers.append({
            "key": key, "label": label, "uri": data_uri(png),
            "battles": counts.get("battles", 0),
            "vehicles": counts.get("vehicles", 0),
            "seconds": counts.get("vehicle_seconds", 0),
        })
    if not layers:
        raise SystemExit(f"no heatmaps found in {heatmaps} with prefix {prefix!r}")

    title = f"{map_meta.get('map', '?')} — {map_meta.get('mode', '?')}"
    buttons = "\n".join(
        f'<button data-layer="{l["key"]}"{" class=\'on\'" if i == 0 else ""}>'
        f'{html.escape(l["label"])}</button>'
        for i, l in enumerate(layers))
    images = "\n".join(
        f'<img class="heat" data-layer="{l["key"]}" src="{l["uri"]}" alt="">'
        for l in layers)
    stats = json.dumps({l["key"]: {"battles": l["battles"], "vehicles": l["vehicles"],
                                   "seconds": l["seconds"]} for l in layers})

    return f"""<!DOCTYPE html>
<meta charset="utf-8">
<title>{html.escape(title)} heatmap</title>
<style>
  :root {{ color-scheme: dark; --fg:#e6ecf2; --muted:#8c9aa7; --bg:#11151a; --panel:#1b222a; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
  header {{ padding:16px 20px 10px; }}
  h1 {{ margin:0; font-size:18px; font-weight:600; }}
  .sub {{ color:var(--muted); font-size:13px; margin-top:2px; }}
  .controls {{ display:flex; flex-wrap:wrap; gap:14px; align-items:center;
               padding:0 20px 14px; }}
  .group {{ display:flex; gap:4px; background:var(--panel); padding:4px;
            border-radius:8px; }}
  button {{ background:transparent; border:0; color:var(--muted); cursor:pointer;
            padding:6px 11px; border-radius:6px; font:inherit; }}
  button:hover {{ color:var(--fg); }}
  button.on {{ background:#2f3b48; color:var(--fg); }}
  label {{ color:var(--muted); display:flex; gap:8px; align-items:center; }}
  input[type=range] {{ width:150px; }}
  .stage {{ position:relative; margin:0 20px 20px; width:min(92vw, 860px);
            aspect-ratio:1/1; background:#000; border-radius:10px; overflow:hidden; }}
  .stage img {{ position:absolute; inset:0; width:100%; height:100%; display:block; }}
  .heat {{ opacity:0; transition:opacity .12s; mix-blend-mode:screen; }}
  .heat.on {{ opacity:var(--heat-opacity, .55); }}
  footer {{ padding:0 20px 28px; color:var(--muted); font-size:12.5px; max-width:70ch; }}
  .legend {{ display:flex; align-items:center; gap:8px; margin-top:8px; }}
  .bar {{ height:9px; width:190px; border-radius:5px;
          background:linear-gradient(90deg,#28469b,#3cb4c8,#5ad26e,#f0c83c,#f04632); }}
</style>

<header>
  <h1>{html.escape(title)}</h1>
  <div class="sub" id="sub"></div>
</header>

<div class="controls">
  <div class="group" id="layers">{buttons}</div>
  <label>Opacity <input type="range" id="opacity" min="0" max="100" value="55"></label>
  <label><input type="checkbox" id="showmap" checked> Map</label>
</div>

<div class="stage" id="stage">
  <img id="base" src="{map_image}" alt="map">
  {images}
</div>

<footer>
  Dwell-time density: each sample is weighted by how long it represents, so a
  vehicle parked in one spot counts for the time it stayed rather than the
  number of samples taken. Routes keep only moving vehicles, Stops only
  near-stationary ones.
  <div class="legend"><span>low</span><span class="bar"></span><span>high</span></div>
</footer>

<script>
  const stats = {stats};
  const stage = document.getElementById('stage');
  const sub = document.getElementById('sub');

  function describe(key) {{
    const s = stats[key];
    if (!s) return '';
    const mins = Math.round(s.seconds / 60);
    return `${{s.battles}} battle${{s.battles === 1 ? '' : 's'}} · ` +
           `${{s.vehicles}} vehicles · ${{mins}} vehicle-minutes`;
  }}

  function select(key) {{
    for (const b of document.querySelectorAll('#layers button'))
      b.classList.toggle('on', b.dataset.layer === key);
    for (const img of document.querySelectorAll('.heat'))
      img.classList.toggle('on', img.dataset.layer === key);
    sub.textContent = describe(key);
  }}

  document.getElementById('layers').addEventListener('click', e => {{
    if (e.target.dataset.layer) select(e.target.dataset.layer);
  }});
  document.getElementById('opacity').addEventListener('input', e => {{
    stage.style.setProperty('--heat-opacity', e.target.value / 100);
  }});
  document.getElementById('showmap').addEventListener('change', e => {{
    document.getElementById('base').style.opacity = e.target.checked ? 1 : 0;
  }});

  stage.style.setProperty('--heat-opacity', 0.55);
  select(document.querySelector('#layers button').dataset.layer);
</script>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map_dir", type=Path)
    parser.add_argument("--heatmaps", type=Path, default=Path("data/heatmaps"))
    parser.add_argument("--prefix", required=True, help="heatmap filename prefix")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(build(args.map_dir, args.heatmaps, args.prefix), encoding="utf-8")
    size = args.out.stat().st_size
    print(f"{args.out} ({size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
