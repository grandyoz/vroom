#!/usr/bin/env python3
"""
Génère une carte HTML comparant deux optimisations VROOM :
  - tracé BLEU  : sans contrainte d'approche
  - tracé ROUGE : avec approach="curb" sur tous les jobs

Usage :
  python3 scripts/generate_map.py \
    --osrm-host localhost --osrm-port 5001 \
    --output /tmp/vroom_map.html
  # puis ouvrir le fichier HTML dans un navigateur
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
VROOM    = ROOT / "bin" / "vroom"

# ---------------------------------------------------------------------------
# Points de la session (lat, lon → VROOM attend [lon, lat])
# ---------------------------------------------------------------------------
JOBS_LATLON = [
    (43.741762, 7.427123),
    (43.742746, 7.428190),
    (43.742350, 7.427782),
    (43.741881, 7.427107),
]
# Dépôt légèrement à l'écart du cluster
DEPOT_LATLON = (43.7410, 43.4255)   # recalculé ci-dessous
DEPOT_LATLON = (43.7408, 7.4255)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def latlon_to_vroom(lat, lon):
    return [lon, lat]


def make_payload(jobs_latlon, with_curb: bool) -> dict:
    depot_ll = DEPOT_LATLON
    jobs = []
    for i, (lat, lon) in enumerate(jobs_latlon, start=1):
        job = {
            "id": i,
            "location": latlon_to_vroom(lat, lon),
            "service": 60,
            "description": f"Stop {i}",
        }
        if with_curb:
            job["approach"] = "curb"
        jobs.append(job)

    return {
        "vehicles": [{
            "id": 1,
            "start": latlon_to_vroom(*depot_ll),
            "end":   latlon_to_vroom(*depot_ll),
            "profile": "driving",
        }],
        "jobs": jobs,
    }


def run_vroom(payload: dict, osrm_host: str, osrm_port: int) -> dict:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        tmp = f.name
    try:
        result = subprocess.run(
            [str(VROOM), "-i", tmp, "-g",
             "-r", "osrm",
             "-a", f"driving:{osrm_host}",
             "-p", f"driving:{osrm_port}"],
            capture_output=True, text=True, timeout=20,
        )
        return json.loads(result.stdout)
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Décodage du polyline encodé (format Google)
# ---------------------------------------------------------------------------

def decode_polyline(s: str) -> list[tuple[float, float]]:
    idx, lat, lng = 0, 0, 0
    coords = []
    while idx < len(s):
        shift = result = 0
        while True:
            b = ord(s[idx]) - 63
            idx += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if result & 1 else result >> 1
        lat += dlat

        shift = result = 0
        while True:
            b = ord(s[idx]) - 63
            idx += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if result & 1 else result >> 1
        lng += dlng

        coords.append((lat / 1e5, lng / 1e5))
    return coords


def extract_route_info(solution: dict) -> tuple[list, list[dict]]:
    """Retourne (liste de coords lat/lon, liste de steps)."""
    if not solution.get("routes"):
        return [], []
    route    = solution["routes"][0]
    geometry = route.get("geometry", "")
    coords   = decode_polyline(geometry) if geometry else []
    steps    = route.get("steps", [])
    return coords, steps


# ---------------------------------------------------------------------------
# Génération HTML / Leaflet
# ---------------------------------------------------------------------------

def build_html(
    jobs_latlon: list,
    depot_latlon: tuple,
    coords_free: list,
    steps_free: list,
    coords_curb: list,
    steps_curb: list,
) -> str:

    # Centrage de la carte sur le barycentre des jobs
    center_lat = sum(ll[0] for ll in jobs_latlon) / len(jobs_latlon)
    center_lon = sum(ll[1] for ll in jobs_latlon) / len(jobs_latlon)

    def steps_to_js(steps, var_name):
        lines = []
        visit_order = 1
        for step in steps:
            if step["type"] not in ("job",):
                continue
            loc = step.get("location")
            if not loc:
                continue
            arr  = step.get("arrival", 0)
            jid  = step.get("job", "?")
            desc = f"Stop {jid} — arrivée {arr}s (ordre visite {visit_order})"
            lines.append(
                f'  {{ lat: {loc[1]}, lon: {loc[0]}, label: "{visit_order}", '
                f'desc: "{desc}", job_id: {jid} }}'
            )
            visit_order += 1
        return f"const {var_name} = [\n" + ",\n".join(lines) + "\n];"

    def polyline_to_js(coords, var_name):
        pts = ", ".join(f"[{lat}, {lon}]" for lat, lon in coords)
        return f"const {var_name} = [{pts}];"

    js_free_line  = polyline_to_js(coords_free, "polyFree")
    js_curb_line  = polyline_to_js(coords_curb, "polyCurb")
    js_free_steps = steps_to_js(steps_free, "stepsFree")
    js_curb_steps = steps_to_js(steps_curb, "stepsCurb")

    job_markers_js = "\n".join(
        f'  L.circleMarker([{lat}, {lon}], '
        f'{{radius: 8, color: "#333", fillColor: "#f5c518", '
        f'fillOpacity: 1, weight: 2}}).addTo(map)'
        f'.bindTooltip("Job {i+1}<br>{lat:.6f}, {lon:.6f}", {{permanent: false}});'
        for i, (lat, lon) in enumerate(jobs_latlon)
    )

    depot_lat, depot_lon = depot_latlon

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8"/>
  <title>VROOM — comparaison approach=curb</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    body {{ margin: 0; font-family: sans-serif; }}
    #map {{ height: 100vh; }}
    .legend {{
      background: white;
      padding: 10px 14px;
      border-radius: 6px;
      box-shadow: 0 1px 5px rgba(0,0,0,.4);
      line-height: 1.8em;
      font-size: 13px;
    }}
    .legend span {{
      display: inline-block;
      width: 28px;
      height: 4px;
      margin-right: 6px;
      vertical-align: middle;
      border-radius: 2px;
    }}
    .dot {{
      width: 12px; height: 12px;
      border-radius: 50%;
      display: inline-block;
      vertical-align: middle;
      margin-right: 6px;
    }}
  </style>
</head>
<body>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ── Données ──────────────────────────────────────────────────────────────
{js_free_line}
{js_curb_line}
{js_free_steps}
{js_curb_steps}

// ── Carte ────────────────────────────────────────────────────────────────
const map = L.map("map").setView([{center_lat}, {center_lon}], 16);

L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
  attribution: "© OpenStreetMap contributors",
  maxZoom: 19,
}}).addTo(map);

// ── Tracés ───────────────────────────────────────────────────────────────
const lineFree = L.polyline(polyFree, {{
  color: "#2979ff", weight: 4, opacity: 0.8,
}}).addTo(map).bindTooltip("Sans contrainte (bleu)");

const lineCurb = L.polyline(polyCurb, {{
  color: "#e53935", weight: 4, opacity: 0.8, dashArray: "8 4",
}}).addTo(map).bindTooltip("Avec approach=curb (rouge)");

// ── Marqueurs d'ordre de visite ───────────────────────────────────────────
function addStepMarkers(steps, color) {{
  steps.forEach(s => {{
    L.circleMarker([s.lat, s.lon], {{
      radius: 11, color: "white", fillColor: color,
      fillOpacity: 1, weight: 2,
    }}).addTo(map)
    .bindTooltip(`<b>Ordre ${{s.label}}</b><br>${{s.desc}}`, {{sticky: true}});

    L.marker([s.lat, s.lon], {{
      icon: L.divIcon({{
        className: "",
        html: `<div style="color:white;font-weight:bold;font-size:11px;
                            text-align:center;line-height:22px;">${{s.label}}</div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      }})
    }}).addTo(map);
  }});
}}

addStepMarkers(stepsFree, "#2979ff");
addStepMarkers(stepsCurb, "#e53935");

// ── Marqueurs des jobs (points d'origine) ────────────────────────────────
{job_markers_js}

// ── Dépôt ────────────────────────────────────────────────────────────────
L.marker([{depot_lat}, {depot_lon}], {{
  icon: L.divIcon({{
    className: "",
    html: `<div style="background:#2e7d32;color:white;font-size:11px;font-weight:bold;
                        padding:3px 6px;border-radius:4px;white-space:nowrap;">Dépôt</div>`,
    iconAnchor: [20, 10],
  }})
}}).addTo(map);

// ── Légende ──────────────────────────────────────────────────────────────
const legend = L.control({{position: "bottomleft"}});
legend.onAdd = () => {{
  const div = L.DomUtil.create("div", "legend");
  div.innerHTML = `
    <b>Comparaison approach=curb</b><br><br>
    <span style="background:#2979ff"></span> Sans contrainte<br>
    <span style="background:#e53935; border-top: 2px dashed #e53935"></span> Avec approach=curb<br>
    <br>
    <span class="dot" style="background:#f5c518;border:2px solid #333"></span> Localisation job<br>
    <span class="dot" style="background:#2979ff"></span> Ordre visite (sans contrainte)<br>
    <span class="dot" style="background:#e53935"></span> Ordre visite (curb)<br>
  `;
  return div;
}};
legend.addTo(map);

// ── Ajuster la vue sur les deux tracés ───────────────────────────────────
const bounds = L.featureGroup([lineFree, lineCurb]).getBounds();
if (bounds.isValid()) map.fitBounds(bounds.pad(0.15));
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--osrm-host", default="localhost")
    parser.add_argument("--osrm-port", type=int, default=5001)
    parser.add_argument("--output",    default="/tmp/vroom_approach_map.html")
    parser.add_argument("--no-open",   action="store_true",
                        help="Ne pas ouvrir le navigateur automatiquement")
    args = parser.parse_args()

    print(f"OSRM : {args.osrm_host}:{args.osrm_port}")
    print(f"Jobs : {len(JOBS_LATLON)} points à Monaco")

    print("\n▶ Optimisation SANS contrainte…")
    sol_free = run_vroom(
        make_payload(JOBS_LATLON, with_curb=False),
        args.osrm_host, args.osrm_port,
    )
    if sol_free.get("code") != 0:
        print(f"Erreur : {sol_free.get('error')}")
        sys.exit(1)
    coords_free, steps_free = extract_route_info(sol_free)
    cost_free = sol_free["summary"]["cost"]
    print(f"  Coût : {cost_free}s — ordre : {[s['job'] for s in steps_free if s['type']=='job']}")

    print("\n▶ Optimisation AVEC approach=curb…")
    sol_curb = run_vroom(
        make_payload(JOBS_LATLON, with_curb=True),
        args.osrm_host, args.osrm_port,
    )
    if sol_curb.get("code") != 0:
        print(f"Erreur : {sol_curb.get('error')}")
        sys.exit(1)
    coords_curb, steps_curb = extract_route_info(sol_curb)
    cost_curb = sol_curb["summary"]["cost"]
    print(f"  Coût : {cost_curb}s — ordre : {[s['job'] for s in steps_curb if s['type']=='job']}")

    diff = cost_curb - cost_free
    print(f"\n  Différence de coût : {diff:+d}s ({'+' if diff>=0 else ''}{diff/cost_free*100:.1f}%)")

    print(f"\n▶ Génération de la carte…")
    html = build_html(
        JOBS_LATLON, DEPOT_LATLON,
        coords_free, steps_free,
        coords_curb, steps_curb,
    )
    out = Path(args.output)
    out.write_text(html, encoding="utf-8")
    print(f"  Carte écrite : {out}")

    if not args.no_open:
        webbrowser.open(f"file://{out.resolve()}")
        print("  Ouverture dans le navigateur…")


if __name__ == "__main__":
    main()
