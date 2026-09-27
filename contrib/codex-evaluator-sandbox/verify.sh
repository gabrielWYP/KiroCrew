#!/usr/bin/env bash
# Verifica la política EFECTIVA de A3 (no etiquetas). Apto para cron sin LLM.
# Exit 0 = todo probado; 1 = algún FAIL; 2 = sin FAIL pero algo no se pudo probar (duda).
# Uso: verify.sh [--live] [--skip-tests] [--skip-runtime] [--skip-e2e] [--no-outer-sandbox]
# Ejecútalo desde una shell del host o un cron (dentro de un agente no se puede leer
# /proc/<gateway>/environ ni anidar bwrap: eso da DOUBT, nunca OK).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ACP=${ACP_DIR:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/lib/node_modules/@agentclientprotocol/codex-acp}
SP=${KIROCREW_SP:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/lib/python3.12/site-packages}
PY=${KIROCREW_PY:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/bin/python}
EVAL_HOME="${EVAL_CODEX_HOME:-${KIROCREW_EVALUATOR_CODEX_HOME:-$HOME/.codex-evaluator}}"
CODEX_BIN=${CODEX_BIN:-$(command -v codex || true)}
[ -n "$CODEX_BIN" ] || { echo "DOUBT codex no está en PATH"; exit 2; }
[ -x "$PY" ] || { echo "DOUBT python de KiroCrew no encontrado: $PY"; exit 2; }
exec "$PY" "$HERE/tools/verify_core.py" --acp "$ACP" --sp "$SP" --eval-home "$EVAL_HOME" \
  --py "$PY" --codex "$CODEX_BIN" "$@"
