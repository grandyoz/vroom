#!/usr/bin/env python3
"""
Vérifie si des coordonnées sont utilisables par OSRM avec et sans approach=curb.

Usage :
  python3 scripts/check_coords.py --osrm-host localhost --osrm-port 5001 \
    "43.730704,7.421280" "43.739990,7.425137" "43.743858,7.429232"
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def check_nearest(host: str, port: int, lon: float, lat: float) -> tuple[bool, str, list]:
    url = f"http://{host}:{port}/nearest/v1/driving/{lon:.6f},{lat:.6f}"
    try:
        d = get(url)
        if d.get("code") != "Ok" or not d.get("waypoints"):
            return False, "hors réseau", []
        w = d["waypoints"][0]
        snapped = w["location"]
        dist = w.get("distance", 0)
        name = w.get("name") or "(route sans nom)"
        return True, f"{name} — snapping à {dist:.0f}m [{snapped[1]:.6f}, {snapped[0]:.6f}]", snapped
    except Exception as e:
        return False, str(e), []


def check_table_curb(host: str, port: int, coords: list[tuple[float, float]]) -> list[bool]:
    """
    Appel table avec approaches=curb sur tous les points.
    Retourne pour chaque point si la durée de sortie est valide (non None).
    """
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    approaches = ";".join(["curb"] * len(coords))
    url = (f"http://{host}:{port}/table/v1/driving/{coord_str}"
           f"?annotations=duration&approaches={approaches}&sources=0&destinations=all")
    try:
        d = get(url)
        if d.get("code") != "Ok":
            return [False] * len(coords)
        durations = d["durations"][0]
        return [v is not None for v in durations]
    except Exception as e:
        print(f"  {RED}Erreur table : {e}{RESET}")
        return [False] * len(coords)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--osrm-host", default="localhost")
    parser.add_argument("--osrm-port", type=int, default=5001)
    parser.add_argument("coords", nargs="+",
                        help='Coordonnées "lat,lon" à tester')
    args = parser.parse_args()

    points: list[tuple[float, float]] = []
    for raw in args.coords:
        try:
            lat, lon = map(float, raw.split(","))
            points.append((lat, lon))
        except ValueError:
            print(f"{RED}Format invalide (attendu lat,lon) : {raw}{RESET}")
            sys.exit(1)

    print(f"\nOSRM : {args.osrm_host}:{args.osrm_port}")
    print(f"Points : {len(points)}\n")

    # ── 1. Vérification nearest ──────────────────────────────────────────────
    print("── Accessibilité (nearest) ─────────────────────────────────────────")
    valid_points = []
    for i, (lat, lon) in enumerate(points, 1):
        ok, msg, snapped = check_nearest(args.osrm_host, args.osrm_port, lon, lat)
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f"  {mark} Point {i} [{lat}, {lon}] → {msg}")
        if ok:
            valid_points.append((lat, lon))

    if not valid_points:
        print(f"\n{RED}Aucun point valide.{RESET}")
        sys.exit(1)

    # ── 2. Vérification approach=curb via table ──────────────────────────────
    print("\n── Compatibilité approach=curb (table) ─────────────────────────────")
    results = check_table_curb(args.osrm_host, args.osrm_port, points)
    all_ok = True
    for i, ((lat, lon), ok) in enumerate(zip(points, results), 1):
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        note = "" if ok else f"  {YELLOW}← approche trottoir impossible ici{RESET}"
        print(f"  {mark} Point {i} [{lat}, {lon}]{note}")
        if not ok:
            all_ok = False

    # ── Résumé ───────────────────────────────────────────────────────────────
    print()
    if all_ok:
        print(f"{GREEN}Tous les points sont compatibles avec approach=curb.{RESET}")
    else:
        print(f"{YELLOW}Certains points ne supportent pas approach=curb.")
        print(f"Déplacez-les légèrement ou retirez la contrainte pour ces jobs.{RESET}")
    print()


if __name__ == "__main__":
    main()
