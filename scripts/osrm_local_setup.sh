#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Déploiement d'une instance OSRM locale avec Monaco (territoire minimal)
# et test de la fonctionnalité approach=curb avec VROOM.
#
# Prérequis : Docker, curl, le binaire VROOM compilé dans ../bin/vroom
#
# Usage :
#   chmod +x osrm_local_setup.sh
#   ./osrm_local_setup.sh          # déploie + teste
#   ./osrm_local_setup.sh --clean  # arrête le conteneur et supprime les données
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VROOM_BIN="$ROOT/bin/vroom"

DATA_DIR="/tmp/osrm_monaco"
PBF="$DATA_DIR/monaco-latest.osm.pbf"
CONTAINER_NAME="vroom_osrm_test"
OSRM_PORT=5001
OSRM_IMAGE="ghcr.io/project-osrm/osrm-backend:latest"
PBF_URL="https://download.geofabrik.de/europe/monaco-latest.osm.pbf"

GREEN="\033[92m"
RED="\033[91m"
YELLOW="\033[93m"
RESET="\033[0m"

info()    { echo -e "${YELLOW}▶ $*${RESET}"; }
success() { echo -e "${GREEN}✓ $*${RESET}"; }
error()   { echo -e "${RED}✗ $*${RESET}" >&2; exit 1; }

# ---------------------------------------------------------------------------
# --clean
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--clean" ]]; then
  info "Arrêt du conteneur $CONTAINER_NAME"
  docker rm -f "$CONTAINER_NAME" 2>/dev/null && success "Conteneur supprimé" || echo "  (pas de conteneur actif)"
  info "Suppression des données dans $DATA_DIR"
  rm -rf "$DATA_DIR" && success "Données supprimées"
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Vérification du binaire VROOM
# ---------------------------------------------------------------------------
info "Vérification du binaire VROOM"
if [[ ! -x "$VROOM_BIN" ]]; then
  info "Binaire absent, compilation en cours…"
  make -C "$ROOT/src" -j"$(nproc)" || error "La compilation a échoué"
fi
success "Binaire VROOM : $VROOM_BIN"

# ---------------------------------------------------------------------------
# 2. Téléchargement du PBF Monaco
# ---------------------------------------------------------------------------
mkdir -p "$DATA_DIR"

if [[ -f "$PBF" && $(stat -c%s "$PBF") -gt 100000 ]]; then
  success "PBF déjà présent ($PBF)"
else
  info "Téléchargement de Monaco depuis Geofabrik (~500 Ko)…"
  curl -L --progress-bar -o "$PBF" "$PBF_URL" \
    || error "Téléchargement impossible. Vérifiez votre connexion internet."
  success "PBF téléchargé : $(du -h "$PBF" | cut -f1)"
fi

# ---------------------------------------------------------------------------
# 3. Pull de l'image OSRM
# ---------------------------------------------------------------------------
info "Récupération de l'image Docker OSRM"
docker pull "$OSRM_IMAGE" || error "Impossible de récupérer l'image OSRM."
success "Image OSRM prête"

# ---------------------------------------------------------------------------
# 4. Prétraitement (extract → partition → customize)
#    Étapes ignorées si les fichiers existent déjà.
# ---------------------------------------------------------------------------
if [[ ! -f "$DATA_DIR/monaco-latest.osrm" ]]; then
  info "Extraction (osrm-extract)…"
  docker run --rm -v "$DATA_DIR:/data" "$OSRM_IMAGE" \
    osrm-extract -p /opt/car.lua /data/monaco-latest.osm.pbf
  success "Extraction terminée"
else
  success "Extraction déjà effectuée"
fi

if [[ ! -f "$DATA_DIR/monaco-latest.osrm.partition" ]]; then
  info "Partitionnement (osrm-partition)…"
  docker run --rm -v "$DATA_DIR:/data" "$OSRM_IMAGE" \
    osrm-partition /data/monaco-latest.osrm
  success "Partitionnement terminé"
else
  success "Partitionnement déjà effectué"
fi

if [[ ! -f "$DATA_DIR/monaco-latest.osrm.cell_metrics" ]]; then
  info "Personnalisation (osrm-customize)…"
  docker run --rm -v "$DATA_DIR:/data" "$OSRM_IMAGE" \
    osrm-customize /data/monaco-latest.osrm
  success "Personnalisation terminée"
else
  success "Personnalisation déjà effectuée"
fi

# ---------------------------------------------------------------------------
# 5. Démarrage du serveur OSRM
# ---------------------------------------------------------------------------
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  success "Conteneur OSRM déjà en cours d'exécution (port $OSRM_PORT)"
else
  info "Démarrage du serveur OSRM sur le port $OSRM_PORT…"
  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
  docker run -d \
    --name "$CONTAINER_NAME" \
    -p "${OSRM_PORT}:5000" \
    -v "$DATA_DIR:/data" \
    "$OSRM_IMAGE" \
    osrm-routed --algorithm mld /data/monaco-latest.osrm

  # Attente du démarrage
  echo -n "  Attente du démarrage"
  for i in $(seq 1 15); do
    sleep 1
    echo -n "."
    if curl -sf "http://localhost:${OSRM_PORT}/health" >/dev/null 2>&1 || \
       curl -sf "http://localhost:${OSRM_PORT}/table/v1/driving/7.416,43.731;7.417,43.732?annotations=duration" >/dev/null 2>&1; then
      break
    fi
  done
  echo ""
  success "Serveur OSRM démarré"
fi

# ---------------------------------------------------------------------------
# 6. Test avec VROOM : approach=curb sur Monaco
#    Points choisis sur le boulevard du Larvotto (route bien orientée)
# ---------------------------------------------------------------------------
info "Test VROOM avec approach=curb (Monaco)"

RESULT=$(cat <<EOF | "$VROOM_BIN" -r osrm -a "driving:localhost" -p "driving:${OSRM_PORT}"
{
  "vehicles": [
    {
      "id": 1,
      "start": [7.4246, 43.7312],
      "end":   [7.4246, 43.7312],
      "profile": "driving"
    }
  ],
  "jobs": [
    {
      "id": 1,
      "location": [7.4188, 43.7285],
      "service": 120,
      "approach": "curb",
      "description": "Arrêt côté trottoir"
    },
    {
      "id": 2,
      "location": [7.4270, 43.7350],
      "service": 120,
      "approach": "curb",
      "description": "Arrêt côté trottoir"
    },
    {
      "id": 3,
      "location": [7.4310, 43.7380],
      "service": 120,
      "description": "Arrêt sans contrainte"
    }
  ]
}
EOF
)

CODE=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('code','?'))")
COST=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('summary',{}).get('cost','?'))")
UNASSIGNED=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('unassigned',[])))")

if [[ "$CODE" == "0" && "$UNASSIGNED" == "0" ]]; then
  success "Solution trouvée — coût total : ${COST}s, non-assignés : $UNASSIGNED"
  echo ""
  echo "  Détail de la route :"
  echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for step in d['routes'][0]['steps']:
    t = step['type']
    loc = step.get('location', [])
    arr = step.get('arrival', 0)
    if loc:
        print(f'    {t:12s} [{loc[0]:.4f}, {loc[1]:.4f}]  arrivée={arr}s')
    else:
        print(f'    {t}')
"
else
  error "Échec de l'optimisation (code=$CODE, non-assignés=$UNASSIGNED)"
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}  Test réussi. Serveur OSRM actif sur localhost:${OSRM_PORT}${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo "  Pour arrêter et nettoyer :"
echo "    $0 --clean"
