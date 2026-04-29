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
    (43.739990, 7.425137),
    (43.743858, 7.429032),
    (43.748571, 7.433140),
]
DEPOT_LATLON = (43.730704, 7.421280)


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
    steps    = route.get("steps", [])
    if geometry:
        coords = decode_polyline(geometry)
    else:
        # Fallback : segments droits entre les étapes (pas de géométrie OSRM)
        coords = [
            (s["location"][1], s["location"][0])
            for s in steps
            if s.get("location")
        ]
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
    cost_free: int = 0,
    cost_curb: int = 0,
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

    diff    = cost_curb - cost_free
    diff_pct = f"{diff/cost_free*100:+.1f}%" if cost_free else ""
    order_free = [s["job"] for s in steps_free if s.get("type") == "job"]
    order_curb = [s["job"] for s in steps_curb if s.get("type") == "job"]

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
    .info-box {{
      background: white;
      padding: 12px 16px;
      border-radius: 6px;
      box-shadow: 0 1px 5px rgba(0,0,0,.4);
      font-size: 13px;
      line-height: 1.9em;
      min-width: 230px;
    }}
    .info-box table {{ border-collapse: collapse; width: 100%; margin-top: 6px; }}
    .info-box td {{ padding: 2px 6px; }}
    .info-box td:first-child {{ font-weight: bold; color: #555; white-space: nowrap; }}
    .swatch {{
      display: inline-block; width: 24px; height: 4px;
      vertical-align: middle; border-radius: 2px; margin-right: 5px;
    }}
    .dot {{
      width: 11px; height: 11px; border-radius: 50%;
      display: inline-block; vertical-align: middle; margin-right: 5px;
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
const map = L.map("map").setView([{center_lat}, {center_lon}], 15);

L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
  attribution: "© OpenStreetMap contributors",
  maxZoom: 19,
}}).addTo(map);

// ── Groupes de couches ────────────────────────────────────────────────────
const grpFree = L.layerGroup();
const grpCurb = L.layerGroup();

// ── Tracés ───────────────────────────────────────────────────────────────
L.polyline(polyFree, {{
  color: "#2979ff", weight: 5, opacity: 0.85,
}}).addTo(grpFree).bindTooltip("Sans contrainte — {cost_free}s");

L.polyline(polyCurb, {{
  color: "#e53935", weight: 5, opacity: 0.85, dashArray: "10 5",
}}).addTo(grpCurb).bindTooltip("Avec approach=curb — {cost_curb}s");

// ── Marqueurs d'ordre de visite ───────────────────────────────────────────
function addStepMarkers(steps, color, grp) {{
  steps.forEach(s => {{
    L.circleMarker([s.lat, s.lon], {{
      radius: 12, color: "white", fillColor: color,
      fillOpacity: 1, weight: 2,
    }}).addTo(grp)
    .bindTooltip(`<b>Ordre ${{s.label}}</b><br>${{s.desc}}`, {{sticky: true}});

    L.marker([s.lat, s.lon], {{
      icon: L.divIcon({{
        className: "",
        html: `<div style="color:white;font-weight:bold;font-size:11px;
                            text-align:center;line-height:24px;">${{s.label}}</div>`,
        iconSize: [24, 24], iconAnchor: [12, 12],
      }})
    }}).addTo(grp);
  }});
}}

addStepMarkers(stepsFree, "#2979ff", grpFree);
addStepMarkers(stepsCurb, "#e53935", grpCurb);

// ── Marqueurs des jobs (points d'origine) — couche fixe ──────────────────
const grpJobs = L.layerGroup().addTo(map);
{job_markers_js.replace('.addTo(map)', '.addTo(grpJobs)')}

// ── Dépôt ────────────────────────────────────────────────────────────────
L.marker([{depot_lat}, {depot_lon}], {{
  icon: L.divIcon({{
    className: "",
    html: `<div style="background:#2e7d32;color:white;font-size:11px;font-weight:bold;
                        padding:3px 7px;border-radius:4px;white-space:nowrap;">Dépôt</div>`,
    iconAnchor: [22, 10],
  }})
}}).addTo(map);

// ── Contrôle de couches ───────────────────────────────────────────────────
grpFree.addTo(map);
grpCurb.addTo(map);

L.control.layers(null, {{
  "🔵 Sans contrainte ({cost_free}s)": grpFree,
  "🔴 Avec approach=curb ({cost_curb}s)": grpCurb,
  "📍 Positions des jobs": grpJobs,
}}, {{ collapsed: false, position: "topright" }}).addTo(map);

// ── Panneau d'info ────────────────────────────────────────────────────────
const info = L.control({{position: "bottomleft"}});
info.onAdd = () => {{
  const div = L.DomUtil.create("div", "info-box");
  div.innerHTML = `
    <b>Comparaison approach=curb</b>
    <table>
      <tr><td></td><td><span class="swatch" style="background:#2979ff"></span>Sans curb</td>
                   <td><span class="swatch" style="background:#e53935"></span>Avec curb</td></tr>
      <tr><td>Coût</td><td>{cost_free}s</td><td>{cost_curb}s</td></tr>
      <tr><td>Écart</td><td colspan="2"><b style="color:#e53935">{diff:+d}s ({diff_pct})</b></td></tr>
      <tr><td>Ordre</td><td>{order_free}</td><td>{order_curb}</td></tr>
    </table>
    <br>
    <span class="dot" style="background:#f5c518;border:2px solid #333"></span> Position job<br>
    Cochez/décochez les couches →
  `;
  return div;
}};
info.addTo(map);

// ── Vue initiale centrée sur les jobs ────────────────────────────────────
const jobBounds = L.latLngBounds([
  {[[f"[{lat},{lon}]" for lat,lon in jobs_latlon]]}
]);
map.fitBounds(jobBounds.pad(0.4));
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
    geom_free = sol_free["routes"][0].get("geometry", "") if sol_free.get("routes") else ""
    print(f"  Coût : {cost_free}s — ordre : {[s['job'] for s in steps_free if s['type']=='job']}")
    print(f"  Géométrie : {len(geom_free)} chars encodés → {len(coords_free)} points décodés")

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
    geom_curb = sol_curb["routes"][0].get("geometry", "") if sol_curb.get("routes") else ""
    print(f"  Coût : {cost_curb}s — ordre : {[s['job'] for s in steps_curb if s['type']=='job']}")
    print(f"  Géométrie : {len(geom_curb)} chars encodés → {len(coords_curb)} points décodés")

    diff = cost_curb - cost_free
    print(f"\n  Différence de coût : {diff:+d}s ({'+' if diff>=0 else ''}{diff/cost_free*100:.1f}%)")

    print(f"\n▶ Génération de la carte…")
    html = build_html(
        JOBS_LATLON, DEPOT_LATLON,
        coords_free, steps_free,
        coords_curb, steps_curb,
        cost_free=cost_free,
        cost_curb=cost_curb,
    )
    out = Path(args.output)
    out.write_text(html, encoding="utf-8")
    print(f"  Carte écrite : {out}")

    if not args.no_open:
        webbrowser.open(f"file://{out.resolve()}")
        print("  Ouverture dans le navigateur…")


if __name__ == "__main__":
    main()
