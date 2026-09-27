#!/usr/bin/env bash
# Revierte A3. Solo restaura si TODO coincide: versión, hash actual == A3 y hash del
# backup .a3orig == stock esperado. Si algo no cuadra, aborta sin tocar nada.
# No borra ~/.codex-evaluator (login/sesiones). NO reinicia el gateway.
# Uso: revert.sh [--dry-run]
set -euo pipefail
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
HERE="$(cd "$(dirname "$0")" && pwd)"
ACP=${ACP_DIR:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/lib/node_modules/@agentclientprotocol/codex-acp}
SP=${KIROCREW_SP:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/lib/python3.12/site-packages}
PY=${KIROCREW_PY:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/bin/python}
J="$HERE/expected.json"
exp() { "$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$J" "$1"; }
expf() { "$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]].get(sys.argv[3],''))" "$J" "$1" "$2"; }
sum() { sha256sum "$1" | cut -d' ' -f1; }
run() { if [ $DRY = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }
die() { echo "ABORT: $*" >&2; exit 1; }

# ---------- preflight
IDX="$ACP/dist/index.js"
ver=$("$PY" -c "import json;print(json.load(open('$ACP/package.json'))['version'])")
ACP_DO=0
case "$(sum "$IDX")" in
  "$(exp codex_acp_index_stock)") echo "codex-acp ya está stock";;
  "$(exp codex_acp_index_patched)")
    [ "$ver" = "$(exp codex_acp_version)" ] || die "codex-acp versión $ver != $(exp codex_acp_version)"
    [ -f "$IDX.a3orig" ] || die "falta $IDX.a3orig"
    [ "$(sum "$IDX.a3orig")" = "$(exp codex_acp_index_stock)" ] || die "$IDX.a3orig no coincide con el stock esperado"
    ACP_DO=1;;
  *) die "codex-acp dist/index.js con hash desconocido (¿actualizado? ¿A2?): no se restaura nada";;
esac
grep -q '^__version__ = "'"$(exp kirocrew_version)"'"' "$SP/kiro_crew/__init__.py" || die "KiroCrew versión inesperada"
KC_DO=0
while read -r h f; do
  cur=$(sum "$SP/$f")
  [ "$cur" = "$h" ] && continue
  [ "$cur" = "$(expf kirocrew_patched "$f")" ] || die "KiroCrew $f hash $cur no es A3: no se restaura nada"
  [ -f "$SP/$f.a3orig" ] || die "falta $SP/$f.a3orig"
  [ "$(sum "$SP/$f.a3orig")" = "$h" ] || die "$SP/$f.a3orig no coincide con el original esperado"
  KC_DO=1
done < "$HERE/kirocrew/orig.sha256"
LOCK="$SP/kiro_crew/acp/evaluator_lock.py"
if [ -e "$LOCK" ] && [ "$(sum "$LOCK")" != "$(expf kirocrew_patched kiro_crew/acp/evaluator_lock.py)" ]; then
  die "$LOCK no es el de A3: no se borra"
fi

# ---------- restore
if [ $ACP_DO = 1 ]; then run cp "$IDX.a3orig" "$IDX"; run rm -f "$IDX.a3orig"; echo "codex-acp restaurado"; fi
if [ $KC_DO = 1 ]; then
  while read -r h f; do
    [ "$(sum "$SP/$f")" = "$h" ] && continue
    run cp "$SP/$f.a3orig" "$SP/$f"; run rm -f "$SP/$f.a3orig"
  done < "$HERE/kirocrew/orig.sha256"
  if [ $DRY = 0 ]; then (cd "$SP" && sha256sum -c --quiet "$HERE/kirocrew/orig.sha256") || die "restauración KiroCrew no verifica"; fi
  echo "KiroCrew restaurado"
else
  echo "KiroCrew ya está stock"
fi
[ -e "$LOCK" ] && run rm -f "$LOCK"
echo "Quita KIROCREW_EVALUATOR_* del entorno del gateway (si los fijaste) y reinícialo. ~/.codex-evaluator no se toca."
