#!/usr/bin/env bash
# Reproduce los tests del lock A3 desde cero: copia kiro_crew instalado a mktemp -d,
# comprueba hashes de partida, aplica kirocrew-evaluator-lock.diff, comprueba hashes
# parcheados y ejecuta tests/test_evaluator_lock.py contra ESA copia.
# No modifica site-packages. Imprime la ruta de la copia (la usan otros scripts).
set -euo pipefail
A3="$(cd "$(dirname "$0")/.." && pwd)"
SP=${KIROCREW_SP:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/lib/python3.12/site-packages}
PY=${KIROCREW_PY:-/mnt/tesis_data/data/miniforge3/envs/kirocrew/bin/python}
T=$(mktemp -d)
cp -a "$SP/kiro_crew" "$T/"
find "$T" -name __pycache__ -type d -prune -exec find {} -mindepth 1 -delete \;
(cd "$T" && sha256sum -c --quiet "$A3/kirocrew/orig.sha256")
git -C "$T" apply -p1 "$A3/kirocrew/kirocrew-evaluator-lock.diff"
(cd "$T" && sha256sum -c --quiet "$A3/kirocrew/patched.sha256")
cd "$A3" && env -u PYTHONPATH PYTHONPATH="$T" "$PY" -m unittest -v tests.test_evaluator_lock
echo "copia usada: $T"
