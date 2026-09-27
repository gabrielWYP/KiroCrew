"""Per-agent ``acp_backend`` resolution for the spawn path (VARIANT B).

kiro-cli lets each ``~/.kiro/agents/<name>.json`` carry a top-level
``acp_backend`` field, but KiroCrew 0.7.1 only ever reads the SINGLE global
``agent.acp_backend`` (from ``config.json``) plus the member-DM auto-route
(``agent.member_acp_backend``). A subagent spawned by ``spawn_run`` therefore
ran on the global backend (kiro-cli) no matter what its own agent JSON said.

This module adds the missing channel: given the agent NAME a spawn resolves to
(``info.agent`` / ``execution.template_id`` — which can be a native skill view
``kirocrew-skill-view-<hash>``, whose projected JSON keeps the field), read that
file's top-level ``acp_backend`` and validate it through the SAME governance /
selectability gate the persisted global field crosses
(:func:`kiro_crew.acp_backends.resolve_selected_backend`).

Design constraints honoured:

* **One gate.** Validation is delegated to ``resolve_selected_backend`` — the
  registry-backed gate — so a per-agent value cannot select a backend the
  global field could not. An invalid / denied / unknown value degrades to the
  global default (this function returns ``None`` and the caller keeps its
  configured default), never to a hard failure.
* **Absent field ⇒ no override.** Only a field that is PRESENT and names a
  different, selectable backend produces an override. A missing field, a field
  equal to kiro, or an unreadable file all return ``None`` so the existing
  precedence (member-DM > configured default) is untouched.
* **Import-light.** Reads only ``config.paths`` (a leaf) and ``acp_backends``
  (a leaf that imports nothing from ``kiro_crew.acp``); safe to import lazily
  from the provider factory body, which runs well after config load.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: An agent name must be a single path segment: no separators, no ``..``. The
#: name comes from a resolved spawn (``info.agent``) or a skill-view alias, both
#: already constrained, but this is defence-in-depth against a hand-crafted name
#: reaching the filesystem read below.
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _agents_dir() -> Path | None:
    """Resolve the user-scope kiro agents directory, or ``None`` if unavailable.

    Delegates to the single accessor every consumer routes through so an
    installed ``KIRO_HOME`` / agents-dir override is honoured here too.
    """
    try:
        from kiro_crew.config.paths import kiro_agents_dir

        return kiro_agents_dir()
    except Exception:  # pragma: no cover - defensive; resolver logs its own reason
        logger.debug("per-agent backend: could not resolve agents dir", exc_info=True)
        return None


def read_agent_acp_backend_field(agent_name: str | None) -> object | None:
    """Return the RAW top-level ``acp_backend`` of ``<agents_dir>/<name>.json``.

    ``None`` when the name is empty / unsafe, the file is missing or unreadable,
    or the field is absent. The value is returned uncoerced so the caller can
    run it through the one selectability gate; a non-string shape a hand-edited
    file can hold is passed through untouched and rejected there.

    RONDA 2 hardening (B#1): the read is delegated to
    :func:`kiro_crew.agent_discovery._read_agent_spec` — the ONE hardened reader
    the package already uses for agent specs — instead of a bare
    ``Path.read_text`` + ``json.loads``. That gate applies the same protections
    every other spec read gets: the ``MAX_FILE_BYTES`` size cap (an oversized
    "agent config" is refused, not slurped), a symlink whose RESOLVED target is
    sensitive (``evil.json`` -> ``~/.aws/credentials``) is refused, AppleDouble
    (``._``) sidecars and non-UTF-8 bytes are skipped, and a non-object document
    is rejected — all folded into ``None``. The ``_AGENT_NAME_RE`` check is kept
    as defence-in-depth anti-traversal on the NAME before it ever reaches the
    filesystem. Every failure still degrades to ``None`` (fail-safe), so the
    caller's precedence (member-DM > configured default) is untouched.
    """
    if not agent_name or not isinstance(agent_name, str):
        return None
    if not _AGENT_NAME_RE.match(agent_name):
        logger.debug("per-agent backend: refusing unsafe agent name %r", agent_name)
        return None
    agents_dir = _agents_dir()
    if agents_dir is None:
        return None
    path = agents_dir / f"{agent_name}.json"
    try:
        # circular-free: agent_discovery is a leaf reader; _read_agent_spec
        # folds every refusal (size cap, sensitive symlink target, AppleDouble,
        # non-UTF-8, non-object) into None and promises not to raise, but any
        # unexpected error is still caught here so this stays fail-safe.
        from kiro_crew.agent_discovery import _read_agent_spec

        data = _read_agent_spec(
            path, operation="per_agent_acp_backend", source="spawn"
        )
    except Exception:  # pragma: no cover - defensive; hardened reader is no-raise
        logger.debug(
            "per-agent backend: hardened read of %s failed", path, exc_info=True
        )
        return None
    if not isinstance(data, dict):
        return None
    return data.get("acp_backend")


def resolve_agent_backend_override(agent_name: str | None) -> str | None:
    """Resolve a per-agent backend OVERRIDE for ``agent_name``, or ``None``.

    Returns a selectable backend string ONLY when the agent JSON names one that
    differs from the default kiro backend; otherwise ``None`` so the caller
    keeps whatever the existing precedence (member-DM > configured default)
    produces. The value crosses the same gate the persisted global field does:

    * a selectable value (``"claude"``, ``"codex"``, ...) ⇒ that value;
    * kiro / absent ⇒ ``None`` (no override, default applies);
    * an unknown / denied / non-string value ⇒ ``None`` with the reason logged
      by :func:`resolve_selected_backend` (degrade, never fail).
    """
    raw = read_agent_acp_backend_field(agent_name)
    if raw is None:
        return None
    # circular-free: acp_backends is a leaf re-export of agent_sdk.backends.
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO, resolve_selected_backend

    resolved = resolve_selected_backend(raw)
    # resolve_selected_backend collapses anything unusable to kiro; a resolved
    # kiro means "no override" whether the file said kiro, said nothing usable,
    # or held a denied value — every one of those keeps the configured default.
    if resolved == ACP_BACKEND_KIRO:
        return None
    logger.info(
        "per-agent backend: agent %r selects acp_backend=%r",
        agent_name,
        resolved,
    )
    return resolved
