#!/usr/bin/env python3
"""
Test suite for the VROOM side-of-road approach constraint feature.

Covers:
  1. Build check (compiles VROOM if the binary is missing or outdated)
  2. Input parsing: valid values ("curb", "unrestricted", absent)
  3. Input parsing: invalid value → explicit error message
  4. OSRM query: approaches= parameter present and correct when curb locations exist
  5. OSRM query: approaches= parameter absent when no curb constraint is needed

Tests 4 and 5 use a lightweight mock OSRM HTTP server so no internet
access or local OSRM installation is required.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
VROOM_BIN = ROOT / "bin" / "vroom"
VROOM_SRC = ROOT / "src"

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

passed = 0
failed = 0


def ok(label):
    global passed
    passed += 1
    print(f"  {GREEN}PASS{RESET}  {label}")


def fail(label, detail=""):
    global failed
    failed += 1
    msg = f"  {RED}FAIL{RESET}  {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)


def section(title):
    print(f"\n{YELLOW}{'─' * 60}{RESET}")
    print(f"{YELLOW}{title}{RESET}")
    print(f"{YELLOW}{'─' * 60}{RESET}")


# ---------------------------------------------------------------------------
# 1. Build
# ---------------------------------------------------------------------------

def build_vroom():
    section("1. Build")

    needs_build = not VROOM_BIN.exists()
    if not needs_build:
        # Rebuild if any source file is newer than the binary.
        bin_mtime = VROOM_BIN.stat().st_mtime
        for src in VROOM_SRC.rglob("*.cpp"):
            if src.stat().st_mtime > bin_mtime:
                needs_build = True
                break
        for src in VROOM_SRC.rglob("*.h"):
            if src.stat().st_mtime > bin_mtime:
                needs_build = True
                break

    if not needs_build:
        ok("Binary up-to-date, skipping build")
        return True

    print(f"  Building VROOM from {VROOM_SRC} …")
    result = subprocess.run(
        ["make", f"-j{os.cpu_count() or 4}"],
        cwd=VROOM_SRC,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        ok("Build succeeded")
        return True
    else:
        fail("Build failed", result.stderr[-500:])
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_vroom(payload: dict, extra_args: list[str] | None = None) -> tuple[int, dict]:
    """Run VROOM with a JSON payload and return (returncode, parsed_output)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(payload, f)
        tmp = f.name
    try:
        cmd = [str(VROOM_BIN), "-i", tmp] + (extra_args or [])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        try:
            output = json.loads(result.stdout or result.stderr)
        except json.JSONDecodeError:
            output = {"raw": result.stdout + result.stderr}
        return result.returncode, output
    finally:
        os.unlink(tmp)


def make_matrix_payload(jobs: list[dict]) -> dict:
    """Build a VROOM payload with a custom 4×4 matrix (no routing engine)."""
    n = len(jobs) + 1  # depot + jobs
    size = 4
    durations = [
        [0,   600, 900,  1200],
        [600, 0,   500,  800],
        [900, 500, 0,    300],
        [1200,800, 300,  0],
    ]
    return {
        "vehicles": [{"id": 1, "start_index": 0, "end_index": 0}],
        "jobs": jobs,
        "matrices": {"car": {"durations": durations}},
    }


# ---------------------------------------------------------------------------
# 2. Input parsing tests (custom matrix, no routing engine)
# ---------------------------------------------------------------------------

def test_parsing():
    section("2. Input parsing")

    # Valid: curb
    rc, out = run_vroom(
        make_matrix_payload([
            {"id": 1, "location_index": 1, "approach": "curb"},
            {"id": 2, "location_index": 2},
        ])
    )
    if rc == 0 and out.get("code") == 0:
        ok('approach="curb" accepted')
    else:
        fail('approach="curb" rejected unexpectedly', str(out))

    # Valid: unrestricted
    rc, out = run_vroom(
        make_matrix_payload([
            {"id": 1, "location_index": 1, "approach": "unrestricted"},
        ])
    )
    if rc == 0 and out.get("code") == 0:
        ok('approach="unrestricted" accepted')
    else:
        fail('approach="unrestricted" rejected unexpectedly', str(out))

    # Valid: absent (default)
    rc, out = run_vroom(
        make_matrix_payload([{"id": 1, "location_index": 1}])
    )
    if rc == 0 and out.get("code") == 0:
        ok("approach absent → default accepted")
    else:
        fail("approach absent raised unexpected error", str(out))

    # Invalid value
    rc, out = run_vroom(
        make_matrix_payload([
            {"id": 1, "location_index": 1, "approach": "left"},
        ])
    )
    error_msg = out.get("error", "")
    if rc != 0 and "left" in error_msg and "curb" in error_msg:
        ok('approach="left" → clear error message')
    else:
        fail('approach="left" should raise an error', str(out))


# ---------------------------------------------------------------------------
# 3. OSRM query tests (mock HTTP server)
# ---------------------------------------------------------------------------

class OsrmMockHandler(BaseHTTPRequestHandler):
    """
    Minimal OSRM mock that:
    - Records the last table and route request URLs.
    - Returns a syntactically valid OSRM response so VROOM doesn't crash.
    """

    captured = {}   # class-level dict shared across requests

    def log_message(self, *args):
        pass  # silence default logging

    def _send_json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if "/table/" in parsed.path:
            OsrmMockHandler.captured["table_url"] = self.path
            # Return a valid 3×3 matrix (depot + 2 jobs)
            n = 3
            durations  = [[i * 600 + j * 300 for j in range(n)] for i in range(n)]
            distances  = [[i * 1000 + j * 500 for j in range(n)] for i in range(n)]
            for i in range(n):
                durations[i][i] = 0
                distances[i][i] = 0
            self._send_json({
                "code": "Ok",
                "durations": durations,
                "distances": distances,
            })

        elif "/route/" in parsed.path:
            OsrmMockHandler.captured["route_url"] = self.path
            self._send_json({
                "code": "Ok",
                "routes": [{
                    "legs": [
                        {"duration": 600, "distance": 1000, "summary": ""},
                        {"duration": 500, "distance": 800,  "summary": ""},
                    ],
                    "distance": 1800,
                    "duration": 1100,
                    "geometry": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
                }],
                "waypoints": [],
            })
        else:
            self.send_response(404)
            self.end_headers()


def start_mock_server() -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), OsrmMockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def osrm_args(port: int) -> list[str]:
    return [
        "-r", "osrm",
        "-a", f"driving:127.0.0.1",
        "-p", f"driving:{port}",
    ]


def make_routing_payload(jobs: list[dict]) -> dict:
    return {
        "vehicles": [
            {
                "id": 1,
                "start": [2.3522, 48.8566],
                "end":   [2.3522, 48.8566],
                "profile": "driving",
            }
        ],
        "jobs": jobs,
    }


def extract_approaches(url: str) -> str | None:
    """Return the value of the approaches= query parameter, or None."""
    qs = parse_qs(urlparse(url).query)
    values = qs.get("approaches")
    return values[0] if values else None


def test_osrm_query():
    section("3. OSRM query construction")

    server, port = start_mock_server()
    try:
        args = osrm_args(port)

        # --- Test A: two curb + one unrestricted ---
        OsrmMockHandler.captured.clear()
        payload = make_routing_payload([
            {"id": 1, "location": [2.3488, 48.8534], "approach": "curb"},
            {"id": 2, "location": [2.3601, 48.8738], "approach": "curb"},
        ])
        rc, out = run_vroom(payload, args)

        table_url = OsrmMockHandler.captured.get("table_url", "")
        approaches = extract_approaches(table_url)

        if approaches is None:
            fail("approaches= absent from table query when curb jobs present", table_url)
        else:
            # Depot (start/end) has no approach constraint, jobs have curb.
            # Expected: unrestricted;curb;curb  (order: depot, job1, job2)
            parts = approaches.split(";")
            curb_count = parts.count("curb")
            unres_count = parts.count("unrestricted")
            if curb_count == 2 and unres_count == 1:
                ok(f"approaches= correct: {approaches}")
            else:
                fail(
                    f"approaches= values unexpected: {approaches}",
                    f"expected 2×curb + 1×unrestricted"
                )

        # --- Test B: no curb constraint ---
        OsrmMockHandler.captured.clear()
        payload = make_routing_payload([
            {"id": 1, "location": [2.3488, 48.8534]},
            {"id": 2, "location": [2.3601, 48.8738]},
        ])
        run_vroom(payload, args)

        table_url = OsrmMockHandler.captured.get("table_url", "")
        approaches = extract_approaches(table_url)

        if approaches is None:
            ok("approaches= absent from table query when no curb constraint")
        else:
            fail(
                "approaches= present but should be absent when no curb",
                f"got: {approaches}"
            )

        # --- Test C: mixed curb + unrestricted ---
        OsrmMockHandler.captured.clear()
        payload = make_routing_payload([
            {"id": 1, "location": [2.3488, 48.8534], "approach": "curb"},
            {"id": 2, "location": [2.3601, 48.8738], "approach": "unrestricted"},
        ])
        run_vroom(payload, args)

        table_url = OsrmMockHandler.captured.get("table_url", "")
        approaches = extract_approaches(table_url)

        if approaches is None:
            fail("approaches= absent when at least one curb job present", table_url)
        else:
            parts = approaches.split(";")
            if "curb" in parts and "unrestricted" in parts:
                ok(f"mixed curb/unrestricted correctly encoded: {approaches}")
            else:
                fail(f"unexpected approaches= value: {approaches}")

    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# 4. Real OSRM integration tests (optional, requires a live OSRM server)
# ---------------------------------------------------------------------------

# Monaco coordinates for real routing tests.
# Points on Boulevard du Larvotto, a well-mapped street.
MONACO_DEPOT  = [7.4246, 43.7312]
MONACO_JOBS   = [
    {"id": 1, "location": [7.4188, 43.7285], "service": 60, "approach": "curb",         "description": "stop A — curb"},
    {"id": 2, "location": [7.4270, 43.7350], "service": 60, "approach": "curb",         "description": "stop B — curb"},
    {"id": 3, "location": [7.4310, 43.7380], "service": 60, "approach": "unrestricted", "description": "stop C — unrestricted"},
    {"id": 4, "location": [7.4152, 43.7256], "service": 60,                              "description": "stop D — default"},
]


def osrm_reachable(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def test_real_osrm(host: str, port: int):
    section(f"4. Intégration OSRM réel ({host}:{port})")

    payload = {
        "vehicles": [{
            "id": 1,
            "start": MONACO_DEPOT,
            "end":   MONACO_DEPOT,
            "profile": "driving",
        }],
        "jobs": MONACO_JOBS,
    }

    args = ["-r", "osrm", "-a", f"driving:{host}", "-p", f"driving:{port}"]
    rc, out = run_vroom(payload, args)

    # Solution trouvée, tous les jobs assignés
    if rc != 0 or out.get("code") != 0:
        fail("Optimisation échouée", out.get("error", str(out)))
        return

    routes    = out.get("routes", [])
    unassigned = out.get("unassigned", [])

    if unassigned:
        fail(f"{len(unassigned)} job(s) non assigné(s)", str(unassigned))
    else:
        ok(f"Tous les jobs assignés — {len(routes)} route(s)")

    if not routes:
        return

    route   = routes[0]
    summary = out.get("summary", {})
    cost    = summary.get("cost", "?")
    duration = summary.get("duration", "?")
    ok(f"Coût total : {cost}s, durée : {duration}s")

    # Vérifie que les étapes ont des heures d'arrivée croissantes
    steps    = route.get("steps", [])
    arrivals = [s["arrival"] for s in steps if "arrival" in s]
    if arrivals == sorted(arrivals):
        ok(f"Heures d'arrivée cohérentes : {arrivals}")
    else:
        fail("Heures d'arrivée non monotones", str(arrivals))

    # Vérifie que la géométrie est demandable sans erreur
    rc2, out2 = run_vroom(payload, args + ["-g"])
    if rc2 == 0 and out2.get("code") == 0:
        has_geom = bool(out2["routes"][0].get("geometry"))
        if has_geom:
            ok("Géométrie (-g) retournée avec approaches=curb")
        else:
            fail("Géométrie absente dans la réponse")
    else:
        fail("Échec avec flag -g (géométrie)", out2.get("error", ""))

    # Affiche le détail de la tournée
    print()
    print("  Détail de la tournée :")
    for step in steps:
        stype = step["type"]
        loc   = step.get("location", [])
        arr   = step.get("arrival", 0)
        jid   = f"  job={step['job']}" if "job" in step else ""
        coord = f"[{loc[0]:.4f}, {loc[1]:.4f}]" if loc else ""
        print(f"    {stype:12s} {coord:26s} arrivée={arr:5d}s{jid}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="VROOM approach constraint test suite")
    parser.add_argument("--osrm-host", default=None,
                        help="Hôte d'un serveur OSRM réel (ex: localhost)")
    parser.add_argument("--osrm-port", type=int, default=5001,
                        help="Port du serveur OSRM réel (défaut: 5001)")
    args = parser.parse_args()

    print(f"\nVROOM approach constraint test suite")
    print(f"Binary : {VROOM_BIN}")

    if not build_vroom():
        print(f"\n{RED}Build failed — aborting tests.{RESET}")
        sys.exit(1)

    test_parsing()
    test_osrm_query()

    # Tests d'intégration contre un vrai OSRM
    osrm_host = args.osrm_host or "localhost"
    osrm_port = args.osrm_port

    if osrm_reachable(osrm_host, osrm_port):
        test_real_osrm(osrm_host, osrm_port)
    else:
        section("4. Intégration OSRM réel (ignorée)")
        print(f"  {YELLOW}Serveur OSRM non joignable sur {osrm_host}:{osrm_port}{RESET}")
        print(f"  Lancer d'abord : scripts/osrm_local_setup.sh")
        print(f"  Puis relancer  : python3 test_approach.py --osrm-host localhost --osrm-port {osrm_port}")

    print(f"\n{'─' * 60}")
    total = passed + failed
    color = GREEN if failed == 0 else RED
    print(f"{color}{passed}/{total} tests passed{RESET}")
    print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
