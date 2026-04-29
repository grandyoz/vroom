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
    Table N×N complète avec approaches=curb — même requête que VROOM.
    Un point est marqué KO si aucune durée valide n'existe depuis/vers lui.
    """
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    approaches = ";".join(["curb"] * len(coords))
    url = (f"http://{host}:{port}/table/v1/driving/{coord_str}"
           f"?annotations=duration&approaches={approaches}")
    try:
        d = get(url)
        if d.get("code") == "NoSegment":
            # Identifie le point fautif via le message d'erreur
            msg = d.get("message", "")
            results = [True] * len(coords)
            for i in range(len(coords)):
                if str(i) in msg:
                    results[i] = False
            return results
        if d.get("code") != "Ok":
            return [False] * len(coords)
        matrix = d["durations"]
        # Un point i est KO si toute sa ligne OU toute sa colonne est None
        ok = []
        for i in range(len(coords)):
            row_ok = any(matrix[i][j] is not None for j in range(len(coords)) if j != i)
            col_ok = any(matrix[j][i] is not None for j in range(len(coords)) if j != i)
            ok.append(row_ok and col_ok)
        return ok
    except Exception as e:
        print(f"  {RED}Erreur table : {e}{RESET}")
        return [False] * len(coords)


def check_single_curb(host: str, port: int, lat: float, lon: float,
                      others: list[tuple[float, float]]) -> bool:
    """Teste si un point isolé est compatible curb dans une table avec les autres points."""
    coords = [(lat, lon)] + others
    results = check_table_curb(host, port, coords)
    return results[0]


def find_nearby_curb(host: str, port: int, lat: float, lon: float,
                     others: list[tuple[float, float]],
                     step: float = 0.0002, radius: int = 4) -> list[tuple[float, float, float]]:
    """Scanne une grille autour du point pour trouver des alternatives compatibles curb."""
    candidates = []
    for dlat in range(-radius, radius + 1):
        for dlon in range(-radius, radius + 1):
            if dlat == 0 and dlon == 0:
                continue
            clat = lat + dlat * step
            clon = lon + dlon * step
            # Vérifie d'abord que le point est sur une route
            ok_nearest, _, _ = check_nearest(host, port, clon, clat)
            if not ok_nearest:
                continue
            if check_single_curb(host, port, clat, clon, others):
                dist_m = ((dlat * step * 111000) ** 2 + (dlon * step * 85000) ** 2) ** 0.5
                candidates.append((clat, clon, dist_m))
    candidates.sort(key=lambda x: x[2])
    return candidates[:5]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--osrm-host", default="localhost")
    parser.add_argument("--osrm-port", type=int, default=5001)
    parser.add_argument("--suggest", action="store_true",
                        help="Cherche des alternatives pour les points incompatibles")
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

    # ── Résumé / suggestions ─────────────────────────────────────────────────
    print()
    if all_ok:
        print(f"{GREEN}Tous les points sont compatibles avec approach=curb.{RESET}")
    else:
        print(f"{YELLOW}Certains points ne supportent pas approach=curb.{RESET}")
        if args.suggest:
            print()
            for i, ((lat, lon), ok) in enumerate(zip(points, results)):
                if ok:
                    continue
                others = [p for j, p in enumerate(points) if j != i]
                print(f"  Recherche d'alternatives pour Point {i+1} [{lat}, {lon}]…")
                alts = find_nearby_curb(args.osrm_host, args.osrm_port, lat, lon, others)
                if alts:
                    for clat, clon, dist in alts:
                        print(f"    {GREEN}→ {clat:.6f}, {clon:.6f}  (~{dist:.0f}m){RESET}")
                else:
                    print(f"    {RED}Aucune alternative trouvée dans un rayon de ~90m{RESET}")
        else:
            print(f"  Relancez avec --suggest pour trouver des alternatives proches.")
    print()


if __name__ == "__main__":
    main()
