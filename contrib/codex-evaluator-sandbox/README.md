# Variante A3: codex-evaluator en modo fail-closed (ronda 3, última)

Parte de `../A2/`. Objetivos: KiroCrew 0.7.1 (con B2 per-agent-backend ya aplicado), codex-acp 1.13.1 y codex-cli 0.157.1, en aarch64 con openat2.
Todo se construyó y probó sobre **copias** en `mktemp -d`. No se ha modificado site-packages, node_modules, `~/.codex` ni `~/.kiro/agents`, y el gateway no se ha reiniciado.

## Hallazgo → cambio → evidencia

| # | Hallazgo (evaluator, ronda 2) | Cambio en A3 | Evidencia |
|---|---|---|---|
| 1 | TOCTOU por rename: grep reutilizaba fds de subdirectorios | `ro_fs_mcp.py` reescrito:<br>• **openat2 obligatorio**; sin él, no se abre ninguna raíz y toda llamada da `fail-closed`. No hay fallback.<br>• **Cada** objeto (read, list y cada dir/archivo de grep) se abre con una sola llamada `openat2(root_fd, "<ruta relativa completa>", BENEATH\|NO_SYMLINKS\|NO_MAGICLINKS\|NO_XDEV)`.<br>• grep encola **rutas**, no fds.<br>• Después de leer o listar, la ruta se re-resuelve desde la raíz y debe dar el mismo `(dev, ino)`. | `evidence/rename-attacks-A2-vs-A3.jsonl`: **A2 filtra en 5/6 ataques deterministas** (openat2 y walk) y 6 veces en la carrera concurrente (walk). **A3 filtra 0**, incluso con la revalidación desactivada. Tests: `test_every_access_is_openat2_from_root_with_full_path`, `TestRenameAttacks` (8), `test_fail_closed_*` (3) |
| 2 | Reapertura O_PATH→nombre; límites incompletos; excepción de `~/.kiro` demasiado amplia | • Apertura **única** `O_RDONLY\|O_NOFOLLOW\|O_NONBLOCK\|O_NOCTTY` vía openat2, y `fstat` sobre ESE fd (`S_ISREG`/`S_ISDIR`) antes de leer. Nunca se reabre.<br>• Límites en todas las operaciones: entradas por directorio (scandir iterativo, se corta en el tope), fds abiertos (contador, ≤2 en uso real), bytes, profundidad, cola, visitas, archivos, hits.<br>• Deadline cooperativo **más** `ITIMER_REAL`, que interrumpe una syscall bloqueada, también en `read_file`.<br>• `~/.kiro/crew/workspace` sólo exceptúa la regla `~/.kiro`; `RO_FS_EXTRA_DENY` y el resto siguen aplicando. | `test_file_opened_once_without_reopen`, `test_fstat_on_the_same_fd_before_reading`, `test_list_entry_limit_iterative` (≤11 entradas consumidas de 500), `test_fd_limit`, `test_no_fd_leak`, `test_read_file_hard_deadline_interrupts_blocked_call`, `test_workspace_exception_lifts_only_the_kiro_rule`. `evidence/tests-ro_fs.txt`: **38/38 OK** |
| 3 | Lock decidido por la identidad del runtime | `kiro_crew/acp/evaluator_lock.py` + ganchos:<br>• Identidad **inmutable por sesión** (`SessionIdentity`, frozen), registrada en `create_session`/`load_session` **antes** de la cola.<br>• El lector del runtime anota qué sesión emitió cada `request_permission`; el hijo interno de un backend se atribuye a su dueño.<br>• `send_response` y `approve_tool` deciden con esa identidad. `rekey` sólo puede **añadir** un lock.<br>• Los alias `kirocrew-skill-view-*` se resuelven: primero el registro en proceso, después el sidecar con sha256 del alias.<br>• `kirocrew` ya se identifica (no se bloquea).<br>• Sigue bloqueado (fail-closed) en codex: alias irresoluble, sin identidad, id ambiguo (el mismo id desde 2 sesiones) y petición no atribuible. | `evidence/lock-per-session-A2-vs-A3.jsonl` (`create_session` real + lector real + pipe real): **A2 bloquea al proposer en el runtime genérico y APRUEBA al evaluator en un runtime de proposer**; A3 decide bien en los 4 escenarios (hermanas, warm pool, runtime ajeno). `tests/test_evaluator_lock.py` **27/27** (hermanas, warm pool/rekey, `load_session` = spawn_continue, lector real, subagente, sharing) |
| 4 | verify con regex y hashes relativos; `--live` débil | • `tools/verify_core.py`: **tomllib** con esquema exacto (claves permitidas, valores, 31 features a `false`, `mcp_servers == {ro_fs}`, `command`/`args`/`env` exactos).<br>• Hashes fijos en `expected.json`: ro_fs instalado frente a expected, no frente a la copia local.<br>• Arranca el ro_fs instalado y habla MCP con él (openat2, 3 herramientas de sólo lectura, raíz listable, `~/.ssh` rechazado).<br>• E2E efectivo.<br>• `--live` exige `end_turn`, las 3 negativas inyectadas **ejecutadas** y rechazadas, y el testigo intacto; sin login da FAIL.<br>• Cualquier excepción da DOUBT. | `evidence/verify-tamper-cases.txt`: **10/10 manipulaciones → exit 1** (`notify`, `[profiles]`, shell on, truco de comentario, ro_fs alterado, args, MCP extra, `PYTHONPATH`, raíz `/`, permisos). `evidence/verify-rehearsal-on-copies.txt`: 74 OK / 1 FAIL correcto / 4 DOUBT. `evidence/verify-installed-current.txt`: exit 1 (A3 no instalado) |
| 5 | La superficie real no estaba probada | • `tools/runtime_e2e.py` usa el **`AcpRuntime` real de KiroCrew** (spawn + `create_session` + `prompt` + `approve_tool` como `parent_policy=auto`) → codex-acp real → `codex app-server` real, con un modelo falso guionizado en 127.0.0.1 que registra el array `tools` y ejecuta intentos de escritura.<br>• KiroCrew fuerza `mcpServers=[]` para toda sesión con lock.<br>• `view_image`, `code_mode`, `code_mode_host`, goals, tool_suggest, `daemon_auto_start` y más quedan apagados en `config.toml`. | `evidence/e2e-runtime-A3-normal.json`: `mcpServers=[]`, herramientas = sólo ro_fs más recursos MCP y `request_user_input`; exec_command/shell/apply_patch/view_image → `unsupported call`; ro_fs lee el testigo; 3 aprobaciones del llamante → decline/cancel/`{}`; testigo intacto; `end_turn`.<br>`evidence/mcp-surface-projection.jsonl`: con **alias**, la proyección real entrega **kirocrew-core y kirocrew-cron (escritura)**; A3 lo anula (`e2e-runtime-A3-alias.json`: `[]`).<br>`evidence/tool-surface.json`: A2 exponía `view_image` y goals |
| 6 | Exportar `CODEX_HOME` al gateway afecta a todos los Codex | • `apply_role_codex_env`: al lanzar codex-acp (`AcpRuntime.spawn` y `AcpClient._spawn`), una identidad con lock recibe `CODEX_HOME=~/.codex-evaluator` (o `KIROCREW_EVALUATOR_CODEX_HOME`).<br>• Si ese home falta, es symlink o es escribible por otros, **se aborta el spawn**; nunca se cae a `~/.codex`.<br>• Se limpian `CODEX_CONFIG`, `INITIAL_AGENT_MODE` y `DISABLE_MCP_CONFIG_FILTERING`.<br>• Los demás Codex nunca heredan ese home.<br>• El evaluator **nunca comparte proceso** (`_should_use_session_sharing_impl`), y un runtime codex no-evaluator **rechaza** una sesión con lock.<br>• Pins opcionales de sha256 para `config.toml` y ro_fs. | E2E: proceso del evaluator `CODEX_HOME=evaluator-home`, `CODEX_CONFIG` ausente; otro agente codex en el mismo test: role `default`, CODEX_HOME ambiental. Stock/A2 (`e2e-runtime-stock.json`, `e2e-runtime-A2.json`): el evaluator usa el CODEX_HOME ambiental, y stock **acepta** las escaladas (`acceptForSession`, escritura concedida). Tests `TestCodexHomeRole` (5), `test_evaluator_session_refused_on_default_codex_process`, `test_load_session_refused_on_default_process`, `TestSharingDecision` |

### Hallazgos nuevos encontrados y cerrados en esta ronda
- **7 (crítico, afecta a stock y a A2): `.codex/config.toml` del repo evaluado.** codex-acp marca el cwd como `trusted`, así que codex carga la capa de config del proyecto. Un candidato podía reactivar `shell_tool`/`view_image` y **lanzar un servidor MCP arbitrario fuera de todo sandbox**. Evidencia: `evidence/e2e-hostile-projcfg-without-untrusted.json`, donde se crea `EVIL_MCP_RAN`.
  - Cambio: segundo hunk en `codex-acp-readonly.diff`. Con `CODEX_ACP_UNTRUSTED_PROJECTS=1` las raíces pasan a `untrusted`, y KiroCrew inyecta esa variable **sólo** al rol evaluator.
  - Resultado: `evidence/e2e-runtime-A3-hostile.json` muestra que no se ejecuta, las herramientas no cambian y el testigo queda intacto. `verify.sh` repite esta prueba en cada ejecución.
- **8: evaluator lanzado con su alias `kirocrew-skill-view-*`.** Recibía kirocrew-core/kirocrew-cron en `session/new`. Queda cerrado por el `mcpServers=[]` forzado (fila 5).
- **Dato de codex 0.157.1:** `unified_exec = false` **se ignora** (`features list` sigue dando true). Lo que retira `exec_command`/`write_stdin` es `shell_tool = false` (`tool-surface.json`, caso `A3+unified_exec_only` = `shell_tool=true`). La afirmación de A2 de que "sin code_mode_host el evaluator se queda sin herramientas" es **falsa**: con `false`, las de ro_fs siguen ahí.

## Contenido
- `ro_fs_mcp.py`: servidor MCP de sólo lectura, versión A3.
- `codex-home-evaluator/config.toml`: plantilla con `@RO_FS_ROOTS@` y `@EVAL_HOME@`. ro_fs se **copia** al propio CODEX_HOME, fuera del workspace compartido.
- `codex-acp-readonly.diff`: modo read-only = `never`/`readOnly`, más el switch untrusted. Hash parcheado `d407081e…`.
- `kirocrew/kirocrew-evaluator-lock.diff`: `acp/evaluator_lock.py` (nuevo), `acp/runtime.py`, `acp/session_handle.py`, `acp/client.py`, `subagent.py` y `subagent_manager/run.py`.
- `kirocrew/orig.sha256` y `kirocrew/patched.sha256`: hashes de partida (stock+B2) y parcheados.
- `expected.json`: hashes fijos (stock, A2 y A3), features exigidas y el hash de ro_fs. Se regenera con `tools/make_expected.py` a partir de ficheros medidos.
- `apply.sh`, `verify.sh`, `revert.sh`.
- `tests/`: `test_ro_fs_mcp.py` (38), `rename_attacks.py`, `test_evaluator_lock.py` (27) y `run_kirocrew_tests.sh`, que reconstruye la copia desde cero.
- `tools/`: `runtime_e2e.py`, `fake_responses_server.py`, `codex_app_server_proxy.py` (registra thread/start, turn/start, config efectiva y arranque MCP, e inyecta aprobaciones), `tool_surface.py`, `mcp_surface.py`, `lock_compare.py`, `rename_attack_compare.py`, `tamper_cases.sh` y `verify_core.py`.

## Aplicar
```bash
cd contrib/codex-evaluator-sandbox
# Si A2 está aplicada, apply.sh aborta: primero  bash ../A2/revert.sh
bash apply.sh --dry-run /ruta/proyecto1:/ruta/proyecto2   # preflight completo, no escribe
bash apply.sh /ruta/proyecto1:/ruta/proyecto2
CODEX_HOME=$HOME/.codex-evaluator codex login --device-auth  # login propio del evaluator
```
- **No** exportes `CODEX_HOME` al gateway.
- Opcional (recomendado): añade al entorno de arranque del gateway los dos pins que imprime `apply.sh`, `KIROCREW_EVALUATOR_CONFIG_SHA256` y `KIROCREW_EVALUATOR_ROFS_SHA256`.
- Reinicia el gateway tú mismo. Si vuelves a ejecutar apply con otras raíces, cambia el hash de `config.toml` y tienes que actualizar el pin.

`apply.sh` aborta sin tocar nada en estos casos: un hash no esperado (A2 incluido), un `.a3orig` manipulado, openat2 no disponible o ninguna raíz utilizable. Ensayo sobre copias en `evidence/apply-revert-rehearsal.txt` (idempotente).

## Verificar
```bash
bash verify.sh            # 0 = probado, 1 = FAIL, 2 = duda. Desde el HOST o un cron sin LLM
bash verify.sh --live     # además, turno REAL con el CODEX_HOME del evaluator
```
Qué comprueba:
- **static:** versiones y hashes.
- **home:** tomllib más el esquema exacto; ro_fs instalado frente a `expected.json`; ro_fs arrancado de verdad; capas `/etc/codex`.
- **runtime:** el gateway **no** exporta el CODEX_HOME del evaluator; el código se cargó después del parche; los procesos codex-acp del evaluator llevan `CODEX_ACP_UNTRUSTED_PROJECTS=1`; faltan pins → DOUBT.
- **e2e sin modelo, 3 escenarios** (normal, `.codex/config.toml` hostil y alias real), a través del KiroCrew y el codex-acp **instalados**, con una copia del `config.toml` instalado.
- **tests.**
- **`--live`:** sin login da FAIL. Exige `end_turn`, las 3 negativas inyectadas ejecutadas y rechazadas, y el testigo intacto. La negativa de escritura a nivel de modelo la prueba el e2e del mismo run: no se ofrece ninguna herramienta de escritura y los intentos dan `unsupported call`.

Para cron: `script`/`command` sin LLM, con alerta si `exit != 0`.

## Revertir
`bash revert.sh [--dry-run]`:
- Exige que el estado actual sea A3 y que los `.a3orig` coincidan con el stock esperado. Si no, aborta sin tocar nada (probado con un backup manipulado: exit 1).
- Restaura byte a byte (`sha256sum -c orig.sha256` OK) y borra `evaluator_lock.py`.
- Después, quita `KIROCREW_EVALUATOR_*` del entorno del gateway y reinícialo. `~/.codex-evaluator` no se borra.

## Qué NO se ha podido cerrar o probar (explícito)
1. **Turno en vivo con el modelo real:** no ejecutado. La única credencial está en `~/.codex`: usarla la modificaría, y copiar `auth.json` pone en riesgo la rotación del refresh token. `verify.sh --live` está escrito; sin login da FAIL. Toda la evidencia e2e usa el pipeline real (KiroCrew → codex-acp → app-server) con un modelo falso guionizado.
2. **Nada instalado y gateway sin reiniciar**, por las reglas. `verify-installed-current.txt` da exit 1 hoy, correctamente.
3. **Dentro del sandbox de agente no se puede anidar bwrap:** los e2e se ejecutaron con `--no-outer-sandbox`, que sólo omite el "sandbox floor" de KiroCrew y queda como DOUBT. `/proc/<gateway>/environ` tampoco se puede leer (DOUBT). Ejecuta `verify.sh` en el host, sin esa opción, para obtener exit 0.

## Riesgos residuales
- **Otros agentes pueden escribir en `$HOME`**, incluido `~/.codex-evaluator`. Mitigación: los pins en el entorno del gateway (KiroCrew se niega a lanzar el evaluator si cambian) y `verify.sh` en cron. Sin pins, una manipulación entre dos ejecuciones de verify no se detecta.
- **ro_fs:**
  - Un hardlink dentro de la raíz hacia un archivo del mismo FS no se detecta por ruta (sólo aplican las reglas por nombre).
  - El FS raíz no es `nodev`: un nodo de dispositivo creado dentro de una raíz (requiere CAP_MKNOD) se *abriría* (`O_NONBLOCK|O_NOCTTY`) antes de que `fstat` lo rechace. No se llega a leer.
  - Con `RESOLVE_NO_XDEV`, un punto de montaje dentro de una raíz no se puede recorrer. Es deliberado.
  - La revalidación estrecha la carrera "sacar y volver a meter", pero no la elimina. El contenido leído siempre estuvo bajo la raíz en algún momento (garantía `path_is_under` del kernel).
- **Lock:**
  - `kirocrew` ahora se identifica. A2 lo bloqueaba; es un cambio de comportamiento.
  - Una sesión con lock en un proceso codex no-evaluator **falla al crearse** en vez de ejecutarse: disponibilidad frente a seguridad, a favor de la seguridad.
  - La resolución de alias lee ficheros pequeños en el loop, sólo para nombres de alias.
  - En `load_session` de backend KAS, el `hoist` posterior no se limpia. El evaluator no es KAS.
  - `lock_compare.py` fuerza `_codex_role="evaluator"` en el runtime compartido para ejercitar el lock. En producción ese caso ni siquiera llega a crearse.
- **Dependencia de versiones:** la semántica de features está probada en codex-cli 0.157.1. El e2e de `verify.sh` vuelve a medir la lista real de herramientas en cada ejecución, así que una actualización que reactive algo da FAIL, no pasa en silencio. Actualizar KiroCrew o codex-acp borra los parches, y verify lo detecta por hash.
- **Siguen expuestas** `request_user_input` y `list/read_mcp_resource(s)`. Son de lectura y ro_fs no publica recursos.
- **El parche de codex-acp** cambia el modo `read-only` para **cualquier** agente codex que lo use (como en A/A2). El switch untrusted sólo lo recibe el evaluator.
