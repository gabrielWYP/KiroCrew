#!/usr/bin/env bash
# Aplica la variante A3. Idempotente. NO reinicia el gateway y NO exporta CODEX_HOME
# a ningún proceso: KiroCrew (parche A3) lo inyecta solo al lanzar codex-acp para
# codex-evaluator*.
# Uso: apply.sh [--dry-run] <raiz-legible-1>[:<raiz-2>...]
#
#  1. codex-acp: modo read-only => approval never + sandbox readOnly, y
#     CODEX_ACP_UNTRUSTED_PROJECTS=1 => raíces "untrusted" (sin .codex/config.toml del repo).
#  2. KiroCrew: lock por sesión + CODEX_HOME por rol + evaluator sin MCP de sesión
#     y siempre en proceso dedicado (backups .a3orig + módulo nuevo).
#  3. CODEX_HOME del evaluator ($HOME/.codex-evaluator): config.toml A3 y ro_fs_mcp.py
#     COPIADO dentro (0400), fuera del workspace compartido.
# Cada paso exige hashes de partida ESPERADOS (stock o A3); cualquier otro valor
# (A2 incluido) aborta sin tocar nada.
set -euo pipefail
DRY=0; [ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
ROOTS="${1:?uso: apply.sh [--dry-run] <raices-legibles separadas por :>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ACP=${ACP_DIR:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/lib/node_modules/@agentclientprotocol/codex-acp}
SP=${KIROCREW_SP:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/lib/python3.12/site-packages}
PY=${KIROCREW_PY:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/bin/python}
EVAL_HOME="${EVAL_CODEX_HOME:-$HOME/.codex-evaluator}"
J="$HERE/expected.json"
exp() { "$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$J" "$1"; }
expf() { "$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]].get(sys.argv[3],''))" "$J" "$1" "$2"; }
sum() { sha256sum "$1" | cut -d' ' -f1; }
run() { if [ $DRY = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }
die() { echo "ABORT: $*" >&2; exit 1; }

# ---------- preflight (todo antes de escribir nada)
case ":$ROOTS:" in *"::"*) die "raíz vacía en '$ROOTS'";; esac
IFS=: read -ra RS <<< "$ROOTS"; for r in "${RS[@]}"; do [[ "$r" = /* ]] || die "raíz no absoluta: $r"; done
[ "$(sum "$HERE/ro_fs_mcp.py")" = "$(exp ro_fs_mcp_sha256)" ] || die "ro_fs_mcp.py de A3 modificado"
[ "$(sum "$HERE/codex-home-evaluator/config.toml")" = "$(exp config_template_sha256)" ] || die "plantilla config.toml modificada"
ver=$("$PY" -c "import json;print(json.load(open('$ACP/package.json'))['version'])")
[ "$ver" = "$(exp codex_acp_version)" ] || die "codex-acp $ver != $(exp codex_acp_version)"
IDX="$ACP/dist/index.js"; H=$(sum "$IDX")
case "$H" in
  "$(exp codex_acp_index_stock)")   ACP_STATE=stock ;;
  "$(exp codex_acp_index_patched)") ACP_STATE=patched ;;
  "$(exp codex_acp_index_A2)")      die "codex-acp tiene el parche A2: ejecuta primero ../A2/revert.sh";;
  *) die "codex-acp dist/index.js hash desconocido $H";;
esac
if [ -e "$IDX.a3orig" ] && [ "$(sum "$IDX.a3orig")" != "$(exp codex_acp_index_stock)" ]; then
  die "$IDX.a3orig existe con hash inesperado"
fi
grep -q '^__version__ = "'"$(exp kirocrew_version)"'"' "$SP/kiro_crew/__init__.py" || die "KiroCrew != $(exp kirocrew_version)"
KC_STATE=""
while read -r h f; do
  cur=$(sum "$SP/$f")
  if [ "$cur" = "$h" ]; then KC_STATE="${KC_STATE}s"
  elif [ "$cur" = "$(expf kirocrew_patched "$f")" ]; then KC_STATE="${KC_STATE}p"
  elif [ -n "$(expf kirocrew_A2 "$f")" ] && [ "$cur" = "$(expf kirocrew_A2 "$f")" ]; then die "KiroCrew $f tiene el parche A2: ejecuta primero ../A2/revert.sh"
  else die "KiroCrew $f hash $cur no es ni stock ni A3"; fi
  [ -e "$SP/$f.a3orig" ] && [ "$(sum "$SP/$f.a3orig")" != "$h" ] && die "$SP/$f.a3orig con hash inesperado"
done < "$HERE/kirocrew/orig.sha256"
LOCK="$SP/kiro_crew/acp/evaluator_lock.py"
if [ -e "$LOCK" ]; then
  case "$(sum "$LOCK")" in
    "$(expf kirocrew_patched kiro_crew/acp/evaluator_lock.py)") ;;
    "$(expf kirocrew_A2 kiro_crew/acp/evaluator_lock.py)") die "evaluator_lock.py es el de A2: ejecuta ../A2/revert.sh";;
    *) die "$LOCK existe con hash inesperado";;
  esac
fi
[[ "$KC_STATE" =~ ^s+$ || "$KC_STATE" =~ ^p+$ ]] || die "KiroCrew parcialmente parcheado ($KC_STATE); revierte a mano"
/usr/bin/python3 -I -c "import runpy,sys;g=runpy.run_path('$HERE/ro_fs_mcp.py',run_name='lib');g['load_roots']('$ROOTS');sys.exit(0 if g['ROOTS'] and g['_probe_openat2']() else 3)" \
  || die "ro_fs: openat2 no disponible o ninguna raíz utilizable en '$ROOTS' (ver mensajes arriba)"
if [ -e "$EVAL_HOME" ]; then
  [ -d "$EVAL_HOME" ] && [ ! -L "$EVAL_HOME" ] && [ -O "$EVAL_HOME" ] || die "$EVAL_HOME no es un directorio propio real"
fi

# ---------- 1) codex-acp
if [ $ACP_STATE = patched ]; then echo "codex-acp ya parcheado (A3)"; else
  run cp -n "$IDX" "$IDX.a3orig"
  run git -C "$ACP" apply -p1 "$HERE/codex-acp-readonly.diff"
  [ $DRY = 1 ] || [ "$(sum "$IDX")" = "$(exp codex_acp_index_patched)" ] || die "hash tras parchear codex-acp inesperado"
  echo "codex-acp parcheado"
fi

# ---------- 2) KiroCrew
if [[ "$KC_STATE" =~ ^p+$ ]] && [ -e "$LOCK" ]; then echo "KiroCrew ya parcheado (A3)"; else
  while read -r _h f; do run cp -n "$SP/$f" "$SP/$f.a3orig"; done < "$HERE/kirocrew/orig.sha256"
  run git -C "$SP" apply -p1 "$HERE/kirocrew/kirocrew-evaluator-lock.diff"
  if [ $DRY = 0 ]; then (cd "$SP" && sha256sum -c --quiet "$HERE/kirocrew/patched.sha256") || die "hash KiroCrew tras parchear inesperado"; fi
  echo "KiroCrew parcheado (requiere reiniciar el gateway para cargarse)"
fi

# ---------- 3) CODEX_HOME del evaluator
run mkdir -p "$EVAL_HOME"; run chmod 700 "$EVAL_HOME"
[ -f "$EVAL_HOME/config.toml" ] && run cp "$EVAL_HOME/config.toml" "$EVAL_HOME/config.toml.bak.$(date +%s)"
run install -m 0400 "$HERE/ro_fs_mcp.py" "$EVAL_HOME/ro_fs_mcp.py"
if [ $DRY = 0 ]; then
  sed -e "s#@EVAL_HOME@#$EVAL_HOME#" -e "s#@RO_FS_ROOTS@#$ROOTS#" "$HERE/codex-home-evaluator/config.toml" > "$EVAL_HOME/config.toml.new"
  chmod 600 "$EVAL_HOME/config.toml.new"; mv -f "$EVAL_HOME/config.toml.new" "$EVAL_HOME/config.toml"
fi
[ -f "$EVAL_HOME/auth.json" ] || echo "FALTA login: CODEX_HOME=$EVAL_HOME codex login --device-auth"
PIN_CFG=$([ $DRY = 1 ] || sum "$EVAL_HOME/config.toml"); PIN_ROFS=$(exp ro_fs_mcp_sha256)
cat <<EOF
Hecho. Pasos manuales (ver README.md):
  - NO exportes CODEX_HOME al gateway (A3 lo inyecta solo para codex-evaluator*).
  - Opcional pero recomendado: fija estos hashes en el entorno de ARRANQUE del gateway
    (KiroCrew se negará a lanzar el evaluator si alguien cambia esos ficheros):
      KIROCREW_EVALUATOR_CONFIG_SHA256=$PIN_CFG
      KIROCREW_EVALUATOR_ROFS_SHA256=$PIN_ROFS
  - Reinicia el gateway tú (sin reinicio el código A3 NO está cargado; verify.sh lo marca FAIL).
  - Verifica: $HERE/verify.sh   (y --live cuando haya login)
EOF
