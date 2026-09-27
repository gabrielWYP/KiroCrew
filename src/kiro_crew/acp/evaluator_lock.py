"""Permission lock + CODEX_HOME role for read-only evaluator sessions (codex-sandbox A3).

``codex-evaluator`` (and ``codex-evaluator-*``) is a READ-ONLY reviewer. Its
adapter (codex-acp) may still send ``session/request_permission`` -- a shell
command, an ``apply_patch`` edit, a ``require_escalated`` retry outside its
sandbox, an MCP tool approval -- and every approval rung Crew has (hook
auto-approve, ``parent_policy=auto``, the interactive card, the gateway
fallback) could answer it with an ALLOW. For these sessions none may.

A3 decides PER SESSION, not per runtime (a runtime can host sibling sessions
of different agents, and a warm-pool rekey rewrites the runtime/handle crew
identity):

* :class:`SessionIdentity` is built ONCE, when the session is created or
  loaded (``AcpRuntime.create_session`` / ``load_session``), from the agent the
  session really runs (``agent or runtime._agent``), its crew identity and the
  runtime backend. It is frozen; later observations (a rekey) can only ADD
  names (:meth:`SessionIdentity.tightened`), never remove one, so a lock can
  be gained but never lost.
* the runtime reader records, for every inbound ``session/request_permission``,
  the identity of the session that EMITTED it (by ``params.sessionId``; a
  backend-internal child is attributed to its owner). ``AcpRuntime.send_response``
  and ``AcpSessionHandle.approve_tool`` decide with THAT identity.

Identity rule (:func:`identity_lock_reason`), fail-closed:

1. any name -- or the authored agent a ``kirocrew-skill-view-*`` alias resolves
   to -- matching ``codex-evaluator`` / ``codex-evaluator-*`` (or an additive
   ``KIROCREW_PERMISSION_LOCK_AGENTS`` pattern) -> locked, on ANY backend;
2. backend ``codex`` and a name containing ``evaluator`` -> locked;
3. backend ``codex`` and a skill-view alias that cannot be resolved to its
   authored agent (in-process projection registry, else the projection's
   metadata sidecar whose sha256 must match the alias file) -> locked;
4. backend ``codex`` and no name at all -> locked.

``kirocrew`` (Crew's default agent) is a POSITIVE identity since A3: a generic
codex client running the default agent is not blocked (A2 blocked it).

The same verdict selects the codex process environment
(:func:`apply_role_codex_env`): a codex process whose identity is locked gets
``CODEX_HOME`` = the evaluator home (``KIROCREW_EVALUATOR_CODEX_HOME``, default
``~/.codex-evaluator``), which must exist and be private, or the spawn FAILS;
every other codex process never inherits that home. Nothing is exported to
the gateway.

Stdlib-only leaf (lazy imports only): imported from acp/client.py,
acp/session_handle.py, acp/runtime.py, subagent.py and
subagent_manager/run.py without cycles.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

CODEX_BACKEND = "codex"
LOCK_ERROR = "evaluator_permission_lock"
EVALUATOR_CODEX_HOME_ENV = "KIROCREW_EVALUATOR_CODEX_HOME"
EVALUATOR_CONFIG_PIN_ENV = "KIROCREW_EVALUATOR_CONFIG_SHA256"
EVALUATOR_ROFS_PIN_ENV = "KIROCREW_EVALUATOR_ROFS_SHA256"
ALIAS_PREFIX = "kirocrew-skill-view-"
# codex-acp reads these process-wide; an evaluator process must not inherit them.
EVALUATOR_SCRUBBED_ENV = ("CODEX_CONFIG", "INITIAL_AGENT_MODE", "DISABLE_MCP_CONFIG_FILTERING")
# ...and gets these. CODEX_ACP_UNTRUSTED_PROJECTS=1 (codex-sandbox codex-acp
# patch) marks the session roots "untrusted", so codex ignores a project-level
# <repo>/.codex/config.toml: the candidate under review cannot re-enable the
# shell or start its own (unsandboxed) MCP server.
EVALUATOR_FORCED_ENV = {"CODEX_ACP_UNTRUSTED_PROJECTS": "1"}

_EVALUATOR_NAME_RE = re.compile(r"^codex-evaluator(?:-[A-Za-z0-9._-]+)?$")
_ALIAS_RE = re.compile(re.escape(ALIAS_PREFIX) + r"[0-9a-f]{24}")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REJECT_OPTION_RE = re.compile(r"^(reject|deny|decline|disallow|cancel|abort|no)([_-].*)?$", re.I)
_SIDECAR_DIR = ".kirocrew-skill-projection-metadata"
_MAX_SPEC_BYTES = 1 << 20

CANCELLED_RESULT = {"outcome": {"outcome": "cancelled"}}

#: Tests point this at a scratch agents dir; production resolves it lazily.
agents_dir_override: Path | None = None


# ------------------------------------------------------------------ identity
@dataclass(frozen=True)
class SessionIdentity:
    """Immutable identity of ONE ACP session, fixed at create/load time."""

    backend: str
    names: tuple[str, ...]
    resolved: tuple[str, ...] = ()
    unresolved_aliases: tuple[str, ...] = ()
    source: str = ""
    extra: tuple[str, ...] = field(default=(), compare=False)

    def tightened(self, *names: Any) -> "SessionIdentity":
        """Same identity plus ``names``; never removes anything."""
        add = tuple(n for n in _names(names) if n not in self.names + self.extra)
        if not add:
            return self
        more = make_identity(self.backend, *add)
        return SessionIdentity(
            self.backend,
            self.names,
            tuple(dict.fromkeys(self.resolved + more.resolved)),
            tuple(dict.fromkeys(self.unresolved_aliases + more.unresolved_aliases)),
            self.source,
            self.extra + add,
        )

    def all_names(self) -> tuple[str, ...]:
        return self.names + self.extra + self.resolved


def _names(identities: Iterable[Any]) -> list[str]:
    return [n.strip() for n in identities if isinstance(n, str) and n.strip()]


def _norm_backend(backend: Any) -> str:
    return backend.strip().lower() if isinstance(backend, str) else ""


def make_identity(backend: Any, *identities: Any, source: str = "") -> SessionIdentity:
    names = tuple(dict.fromkeys(_names(identities)))
    resolved, unresolved = [], []
    for n in names:
        if n.startswith(ALIAS_PREFIX):
            real = resolve_alias(n)
            if real:
                resolved.append(real)
            else:
                unresolved.append(n)
    return SessionIdentity(_norm_backend(backend), names, tuple(resolved), tuple(unresolved), source)


# ------------------------------------------------------------------ alias resolution
def _agents_dir() -> Path | None:
    if agents_dir_override is not None:
        return agents_dir_override
    try:
        from kiro_crew.config.paths import kiro_agents_dir

        return Path(kiro_agents_dir())
    except Exception:
        return None


def _read_regular(path: Path) -> bytes | None:
    """O_NOFOLLOW read of a regular file, size-capped; None on anything odd."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_SPEC_BYTES:
            return None
        chunks, left = [], _MAX_SPEC_BYTES + 1
        while left > 0:
            b = os.read(fd, min(65536, left))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        return None if len(data) > _MAX_SPEC_BYTES else data
    except OSError:
        return None
    finally:
        os.close(fd)


def resolve_alias(alias: str) -> str | None:
    """Authored agent behind a ``kirocrew-skill-view-<24 hex>`` alias, or None."""
    if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
        return None
    # 1. In-process projection registry (the gateway that spawned the alias).
    try:
        from kiro_crew.acp import skill_projection as _sp

        with _sp._ACTIVE_PROJECTIONS_LOCK:
            projections = tuple(_sp._ACTIVE_PROJECTIONS.values())
        for p in projections:
            for name, al in p.aliases.items():
                if al == alias and isinstance(name, str) and _SAFE_NAME_RE.match(name):
                    return name
    except Exception:
        pass
    # 2. The projection's own sidecar, bound to the alias file by sha256.
    d = _agents_dir()
    if d is None:
        return None
    meta_raw = _read_regular(d / _SIDECAR_DIR / f"{alias}.json")
    spec_raw = _read_regular(d / f"{alias}.json")
    if meta_raw is None or spec_raw is None:
        return None
    try:
        meta = json.loads(meta_raw)
    except ValueError:
        return None
    if not isinstance(meta, dict) or meta.get("x-kirocrew-managed") != "skill-view":
        return None
    if meta.get("x-kirocrew-alias-sha256") != hashlib.sha256(spec_raw).hexdigest():
        return None
    name = meta.get("x-kirocrew-agent")
    if not isinstance(name, str) or not _SAFE_NAME_RE.match(name) or name.startswith(ALIAS_PREFIX):
        return None
    return name


# ------------------------------------------------------------------ verdicts
def _extra_patterns() -> list[re.Pattern[str]]:
    """Additive-only extra agent-name regexes (``KIROCREW_PERMISSION_LOCK_AGENTS``,
    comma-separated). There is deliberately NO knob that removes the lock."""
    out = []
    for raw in os.environ.get("KIROCREW_PERMISSION_LOCK_AGENTS", "").split(","):
        raw = raw.strip()
        if raw:
            try:
                out.append(re.compile(raw))
            except re.error:
                logger.warning("evaluator_lock: ignoring invalid pattern %r", raw)
    return out


def identity_lock_reason(ident: SessionIdentity | None) -> str | None:
    """Why every permission request of this session must be rejected, or None."""
    if ident is None:
        return None
    extra = _extra_patterns()
    for n in ident.all_names():
        if _EVALUATOR_NAME_RE.match(n) or any(p.fullmatch(n) for p in extra):
            return f"agent {n!r} is a locked read-only evaluator"
    if ident.backend != CODEX_BACKEND:
        return None
    for n in ident.all_names():
        if "evaluator" in n.lower():
            return f"codex backend, evaluator-role agent {n!r}"
    if ident.unresolved_aliases:
        return f"codex backend, unresolvable alias {ident.unresolved_aliases[0]!r} (fail-closed)"
    if not ident.names and not ident.extra:
        return "codex backend with no agent identity (fail-closed)"
    return None


def permission_lock_reason(backend: Any, *identities: Any) -> str | None:
    """Convenience: verdict for an identity built from loose names."""
    return identity_lock_reason(make_identity(backend, *identities))


def is_evaluator_agent(name: Any) -> bool:
    """True when ``name`` (or the agent its alias resolves to) is an evaluator."""
    if not isinstance(name, str) or not name.strip():
        return False
    ident = make_identity("", name)
    extra = _extra_patterns()
    return any(_EVALUATOR_NAME_RE.match(n) or any(p.fullmatch(n) for p in extra)
               for n in ident.all_names())


def is_allow_outcome(result: Any) -> bool:
    """True when a JSON-RPC result is a permission answer that is NOT a reject."""
    if not isinstance(result, dict):
        return False
    outcome = result.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("outcome") != "selected":
        return False
    option_id = outcome.get("optionId")
    return not (isinstance(option_id, str) and _REJECT_OPTION_RE.match(option_id))


def backend_of(*objs: Any) -> str:
    for o in objs:
        if o is None:
            continue
        for attr in ("backend", "acp_backend", "_acp_backend"):
            try:
                v = getattr(o, attr, None)
            except Exception:  # a property may raise on a half-built object
                v = None
            if isinstance(v, str) and v:
                return v
    return ""


def subagent_lock_reason(client: Any, request_id: Any, agent_name: Any) -> str | None:
    """Verdict for a subagent approval rung (``SubagentManager._approve_and_log``).

    The subagent's own agent name (fixed for the run) locks on its own; then
    the object that will answer decides PER REQUEST: a session handle (shared
    runtime, warm pool, spawn_continue) or a dedicated AcpClient. Never the
    runtime's identity. With nothing that can decide, fail closed on codex.
    """
    inner = getattr(client, "_client", None)
    backend = backend_of(client, inner, getattr(client, "_runtime", None))
    if isinstance(agent_name, str) and agent_name.strip():
        r = permission_lock_reason(backend, agent_name)
        if r:
            return r
    decided = False
    for obj in (client, inner):
        if obj is None:
            continue
        handle_fn = getattr(getattr(obj, "_handle", None), "_evaluator_lock_reason", None)
        if callable(handle_fn):
            decided = True
            r = handle_fn(request_id)
            if isinstance(r, str) and r:
                return r
        client_fn = getattr(obj, "_evaluator_lock_reason", None)
        if callable(client_fn):
            decided = True
            r = client_fn()
            if isinstance(r, str) and r:
                return r
    if not decided:
        return permission_lock_reason(backend, agent_name)
    return None


def request_key(request_id: Any) -> str:
    """One key per JSON-RPC id whatever its JSON type (callers may stringify)."""
    return str(request_id)


def log_lock(where: str, request_id: Any, reason: str) -> None:
    logger.warning(
        "evaluator permission lock: %s rejected permission request id=%s (%s)",
        where,
        request_id,
        reason,
    )


# ------------------------------------------------------------------ per-runtime bookkeeping
AMBIGUOUS = SessionIdentity("", ("<ambiguous>",), source="ambiguous")
_MAX_TRACKED_REQUESTS = 4096


class PermissionOrigins:
    """request id -> identity of the session that emitted it (bounded)."""

    def __init__(self) -> None:
        self._by_key: dict[str, SessionIdentity | None] = {}

    def record(self, request_id: Any, ident: SessionIdentity | None) -> None:
        k = request_key(request_id)
        if k in self._by_key and self._by_key[k] != ident:
            ident = AMBIGUOUS  # same id seen from two sessions: attribute to nobody
        self._by_key[k] = ident
        while len(self._by_key) > _MAX_TRACKED_REQUESTS:
            self._by_key.pop(next(iter(self._by_key)))

    def lookup(self, request_id: Any) -> tuple[bool, SessionIdentity | None]:
        k = request_key(request_id)
        return (k in self._by_key, self._by_key.get(k))

    def forget(self, request_id: Any) -> None:
        self._by_key.pop(request_key(request_id), None)


def request_lock_reason(
    backend: Any,
    origins: PermissionOrigins,
    registered: Iterable[SessionIdentity],
    request_id: Any,
    fallback: SessionIdentity | None = None,
) -> str | None:
    """Verdict for ONE permission request on a runtime.

    Known origin -> that session's identity (plus ``fallback``, the session
    answering it: either one locked is enough). Unknown origin -> the answering
    session's identity. Ambiguous origin, or no identity at all -> fail closed
    on codex, and on any backend if some session of this runtime is locked
    (the request cannot be shown not to be that session's).
    """
    seen, ident = origins.lookup(request_id)
    if seen and ident is not None and ident is not AMBIGUOUS:
        return identity_lock_reason(ident) or identity_lock_reason(fallback)
    r = identity_lock_reason(fallback)
    if r:
        return r
    if fallback is not None and not (seen and ident is AMBIGUOUS):
        return None
    if _norm_backend(backend) == CODEX_BACKEND:
        return "codex backend, permission request not attributable to a registered session (fail-closed)"
    for other in registered:
        r = identity_lock_reason(other)
        if r:
            return f"unattributable request on a runtime hosting a locked session ({r})"
    return None


# ------------------------------------------------------------------ CODEX_HOME by role
class EvaluatorSandboxError(RuntimeError):
    """A locked codex identity cannot get its dedicated CODEX_HOME: refuse to spawn."""


def evaluator_codex_home() -> Path:
    raw = os.environ.get(EVALUATOR_CODEX_HOME_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".codex-evaluator"


def evaluator_home_problem(home: Path) -> str | None:
    """None when ``home`` is usable as the evaluator's CODEX_HOME."""
    try:
        st = os.lstat(home)
    except OSError as e:
        return f"{home} missing ({e.strerror})"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return f"{home} is not a real directory"
    if st.st_uid != os.getuid():
        return f"{home} not owned by uid {os.getuid()}"
    if st.st_mode & 0o022:
        return f"{home} is group/other-writable"
    try:
        cst = os.lstat(home / "config.toml")
    except OSError:
        return f"{home}/config.toml missing"
    if not stat.S_ISREG(cst.st_mode) or cst.st_uid != os.getuid() or cst.st_mode & 0o022:
        return f"{home}/config.toml is not a private regular file"
    return _pin_problem(home)


def _pin_problem(home: Path) -> str | None:
    """Optional pins (set in the GATEWAY's start environment, which no agent can
    rewrite): KIROCREW_EVALUATOR_CONFIG_SHA256 for config.toml and
    KIROCREW_EVALUATOR_ROFS_SHA256 for <home>/ro_fs_mcp.py (the server the
    config must launch). A mismatch refuses the spawn."""
    want_cfg = os.environ.get(EVALUATOR_CONFIG_PIN_ENV, "").strip().lower()
    want_rofs = os.environ.get(EVALUATOR_ROFS_PIN_ENV, "").strip().lower()
    for want, path in ((want_cfg, home / "config.toml"), (want_rofs, home / "ro_fs_mcp.py")):
        if not want:
            continue
        raw = _read_regular(path)
        if raw is None:
            return f"{path} unreadable for its pinned sha256"
        got = hashlib.sha256(raw).hexdigest()
        if got != want:
            return f"{path} sha256 {got[:16]}... does not match the pinned {want[:16]}..."
    return None


def _same_path(a: str | None, b: Path) -> bool:
    if not a:
        return False
    try:
        return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(b)
    except Exception:
        return False


def apply_role_codex_env(env: dict[str, str], backend: Any, *identities: Any) -> str:
    """Select the codex process environment by role. Mutates ``env``.

    Returns ``"evaluator"`` (CODEX_HOME forced to the evaluator home, codex-acp
    config env scrubbed, project config layers untrusted), ``"default"`` (a codex process that must never see
    the evaluator home) or ``"n/a"`` (not a codex backend; untouched).
    Raises :class:`EvaluatorSandboxError` when a locked identity cannot get a
    usable evaluator home -- the evaluator never falls back to ``~/.codex``.
    """
    if _norm_backend(backend) != CODEX_BACKEND:
        return "n/a"
    home = evaluator_codex_home()
    reason = identity_lock_reason(make_identity(backend, *identities))
    if reason:
        problem = evaluator_home_problem(home)
        if problem:
            raise EvaluatorSandboxError(
                f"refusing to start codex for a locked evaluator identity ({reason}): {problem}. "
                f"Create it with apply.sh (codex-sandbox A3) or set {EVALUATOR_CODEX_HOME_ENV}."
            )
        env["CODEX_HOME"] = str(home)
        for k in EVALUATOR_SCRUBBED_ENV:
            env.pop(k, None)
        env.update(EVALUATOR_FORCED_ENV)
        return "evaluator"
    if _same_path(env.get("CODEX_HOME"), home):
        env.pop("CODEX_HOME", None)
    return "default"
