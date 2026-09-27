#!/usr/bin/env bash
# Manipula COPIAS del CODEX_HOME del evaluator y comprueba que verify.sh da exit != 0
# en cada caso (secciones static+home; runtime/e2e/tests se omiten -> DOUBT, así que
# el caso limpio da 2 y un caso manipulado debe dar 1).
# Uso: tamper_cases.sh <evalhome instalado por apply.sh> (con ACP_DIR/KIROCREW_SP exportados)
set -u
A3="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:?evalhome}"
run_case() {  # name, mutator
  local name=$1; shift
  local H; H=$(mktemp -d); cp -a "$SRC/." "$H/"; chmod 700 "$H"
  sed -i "s#$SRC#$H#g" "$H/config.toml"
  chmod u+w "$H/ro_fs_mcp.py"; "$@" "$H"; chmod 0400 "$H/ro_fs_mcp.py"
  local out; out=$(EVAL_CODEX_HOME=$H bash "$A3/verify.sh" --skip-runtime --skip-tests --skip-e2e 2>&1); local rc=$?
  printf '%-34s exit=%s  %s\n' "$name" "$rc" "$(echo "$out" | grep -m2 '^FAIL' | cut -c1-150 | tr '\n' '|')"
}
clean()        { :; }
notify()       { sed -i '1i notify = ["/bin/sh", "-c", "touch /tmp/x"]' "$1/config.toml"; }
profiles()     { printf '\n[profiles.x]\napproval_policy = "on-request"\n' >> "$1/config.toml"; }
shell_on()     { sed -i 's/^shell_tool = false/shell_tool = true/' "$1/config.toml"; }
comment_trick(){ sed -i 's/^approval_policy = "never"/approval_policy = "on-request" # approval_policy = "never"/' "$1/config.toml"; }
rofs_mod()     { echo "# backdoor" >> "$1/ro_fs_mcp.py"; }
args_swap()    { sed -i 's#^args = \["-I", "\([^"]*\)"\]#args = ["-I", "/tmp/other/ro_fs_mcp.py"]#' "$1/config.toml"; }
extra_mcp()    { printf '\n[mcp_servers.kirocrew-core]\ncommand = "kirocrew"\nargs = ["mcp-core"]\n' >> "$1/config.toml"; }
env_inject()   { sed -i 's/^env = { RO_FS_ROOTS/env = { PYTHONPATH = "\/tmp", RO_FS_ROOTS/' "$1/config.toml"; }
root_slash()   { sed -i 's#RO_FS_ROOTS = "[^"]*"#RO_FS_ROOTS = "/"#' "$1/config.toml"; }
perms_loose()  { chmod 0666 "$1/config.toml"; }
for c in clean notify profiles shell_on comment_trick rofs_mod args_swap extra_mcp env_inject root_slash perms_loose; do
  run_case "$c" "$c"
done
