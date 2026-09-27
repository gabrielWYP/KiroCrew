"""Tests for per-agent ``acp_backend`` selection on the spawn path.

An agent JSON may declare ``"acp_backend"`` (e.g. ``claude`` / ``codex``); a
subagent spawned from it runs on that backend instead of the gateway-global
one. The agent-JSON read goes through ``agent_discovery._read_agent_spec`` (size
cap, sensitive-symlink refusal) and the name is guarded against traversal.
Every failure degrades to ``None`` (global default applies).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import kiro_crew  # noqa: E402
from kiro_crew import agent_backend_resolver as abr  # noqa: E402
from kiro_crew.members import select_provider_backend  # noqa: E402



# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the resolver's agents-dir accessor at a temp directory.

    Installs a ``_agents_dir_override`` on ``config.paths`` — the single
    accessor ``kiro_agents_dir()`` (and thus the resolver) routes through.
    """
    from kiro_crew.config import paths as cfg_paths

    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(cfg_paths, "_agents_dir_override", lambda: d)
    return d


def _write_agent(agents_dir: Path, name: str, spec: dict) -> None:
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


# ── resolve_agent_backend_override ──────────────────────────────────────────


def test_claude_agent_resolves_claude(agents_dir):
    _write_agent(
        agents_dir,
        "claude-proposer",
        {"name": "claude-proposer", "acp_backend": "claude", "model": "auto"},
    )
    assert abr.resolve_agent_backend_override("claude-proposer") == "claude"


def test_codex_agent_resolves_codex(agents_dir):
    _write_agent(
        agents_dir,
        "codex-evaluator",
        {"name": "codex-evaluator", "acp_backend": "codex", "model": "auto"},
    )
    assert abr.resolve_agent_backend_override("codex-evaluator") == "codex"


def test_no_field_means_no_override(agents_dir):
    _write_agent(agents_dir, "plain", {"name": "plain", "model": "auto"})
    assert abr.resolve_agent_backend_override("plain") is None


def test_explicit_kiro_means_no_override(agents_dir):
    # kiro IS the default; naming it explicitly must not force a dedicated
    # override that behaves any differently from the global default.
    _write_agent(agents_dir, "kiroagent", {"name": "kiroagent", "acp_backend": ""})
    assert abr.resolve_agent_backend_override("kiroagent") is None


def test_invalid_value_degrades_to_no_override(agents_dir):
    # An unknown/denied value crosses resolve_selected_backend, which collapses
    # it to kiro; the resolver reports that as "no override" (degrade, no crash).
    _write_agent(
        agents_dir, "bogus", {"name": "bogus", "acp_backend": "not-a-real-backend"}
    )
    assert abr.resolve_agent_backend_override("bogus") is None


def test_non_string_value_degrades(agents_dir):
    _write_agent(agents_dir, "weird", {"name": "weird", "acp_backend": 123})
    assert abr.resolve_agent_backend_override("weird") is None


def test_missing_file_is_none(agents_dir):
    assert abr.resolve_agent_backend_override("does-not-exist") is None


def test_unsafe_name_refused(agents_dir):
    # Defence-in-depth: a name with path separators must never reach the fs read.
    assert abr.resolve_agent_backend_override("../escape") is None
    assert abr.resolve_agent_backend_override("a/b") is None
    assert abr.resolve_agent_backend_override("") is None
    assert abr.resolve_agent_backend_override(None) is None


def test_skill_view_alias_resolves_same(agents_dir):
    # A native skill-view projection keeps the top-level acp_backend; its file
    # is named for the alias, so the same by-name read resolves it.
    alias = "kirocrew-skill-view-02e2bb413fdfe8d25695a7f6"
    _write_agent(agents_dir, alias, {"name": alias, "acp_backend": "codex"})
    assert abr.resolve_agent_backend_override(alias) == "codex"


# ── B2 hardening: the read now crosses the hardened spec reader ──────────────


def test_oversized_json_returns_none(agents_dir, monkeypatch):
    """An oversized agent JSON is refused by the reader's size cap → None.

    The real cap is 50 MB (``hooks.MAX_FILE_BYTES``); shrink it so the test
    file stays tiny. The read routes through ``_read_agent_spec`` →
    ``_read_spec_bytes``, which reads ``cap + 1`` bytes and raises
    ``FileTooLargeError`` past the cap; the resolver folds that into ``None``.
    """
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 64)
    # A well-formed spec that names a real backend, but larger than the cap:
    padded = {"name": "big", "acp_backend": "claude", "pad": "x" * 200}
    _write_agent(agents_dir, "big", padded)
    assert (agents_dir / "big.json").stat().st_size > 64  # precondition
    # Without the size cap this would resolve to 'claude'; with it, refused.
    assert abr.resolve_agent_backend_override("big") is None


def test_symlink_to_sensitive_target_returns_none(agents_dir, monkeypatch):
    """A ``<name>.json`` symlink whose RESOLVED target is sensitive → None.

    ``_read_agent_spec`` resolves the path ``strict=True`` and runs the result
    through ``_fence_refuses``; a resolved sensitive target is refused before
    any bytes are read. We monkeypatch the fence to mark our decoy target
    sensitive so the test does not depend on the real home layout, and point a
    valid-looking agent symlink at it. The decoy holds a real backend value, so
    only the fence — not a parse failure — can be what returns ``None``.
    """
    from kiro_crew import agent_discovery

    secret = agents_dir.parent / "pretend_credentials.json"
    secret.write_text(
        json.dumps({"name": "sneaky", "acp_backend": "claude"}), encoding="utf-8"
    )
    link = agents_dir / "sneaky.json"
    os.symlink(secret, link)

    real_fence = agent_discovery._fence_refuses

    def _fence(real: Path) -> bool:
        if Path(real) == secret.resolve():
            return True
        return real_fence(real)

    monkeypatch.setattr(agent_discovery, "_fence_refuses", _fence)
    # Sanity: the link and target are otherwise readable/valid; only the fence
    # verdict is what must produce None.
    assert abr.resolve_agent_backend_override("sneaky") is None


def test_name_with_dotdot_returns_none(agents_dir):
    """A name containing ``../`` is refused by the anti-traversal regex → None.

    ``_AGENT_NAME_RE`` (kept from B as defence-in-depth) rejects any name with a
    path separator or ``..`` before the filesystem is ever touched, even though
    the hardened reader would also refuse the resolved target.
    """
    assert abr.resolve_agent_backend_override("../../etc/passwd") is None
    assert abr.resolve_agent_backend_override("..") is None
    assert abr.resolve_agent_backend_override("foo/../bar") is None
    # And the RAW reader (the layer that touches the fs) refuses it too.
    assert abr.read_agent_acp_backend_field("../../etc/passwd") is None


# ── select_provider_backend precedence ──────────────────────────────────────


def test_override_beats_configured_default():
    # Non-member session: per-agent override wins over the configured default.
    got = select_provider_backend(
        session_key="task-abc",
        member_backend="kas",
        configured_default="",  # kiro
        agent_backend_override="claude",
    )
    assert got == "claude"


def test_no_override_falls_through_to_default():
    got = select_provider_backend(
        session_key="task-abc",
        member_backend="kas",
        configured_default="codex",
        agent_backend_override=None,
    )
    assert got == "codex"


def test_member_dm_still_wins_over_override(monkeypatch):
    # A member-DM session key must still take the member auto-route even when a
    # per-agent override is present: the live session-scoped decision wins.
    import kiro_crew.members as members

    monkeypatch.setattr(members, "is_member_session_key", lambda key: True)
    got = members.select_provider_backend(
        session_key="dm_slot_whatever",
        member_backend="codex",  # the member auto-route
        configured_default="",
        agent_backend_override="claude",  # present, but must lose
    )
    assert got == "codex"


def test_default_signature_backward_compatible():
    # The new param is optional; a 3-arg call (the pre-patch shape) still works.
    assert select_provider_backend("task-x", "kas", "claude") == "claude"


# ── session-sharing gate: backend mismatch forces a dedicated process ────────


class _FakeProvider:
    def __init__(self, backend):
        self.backend = backend


class _FakeSessions:
    def __init__(self, parent_backend):
        self._parent_backend = parent_backend

    def is_session_sharing_eligible(self, key):
        return True

    def get_provider(self, key):
        return _FakeProvider(self._parent_backend)


class _FakeManager:
    def __init__(self, parent_backend):
        self._sessions = _FakeSessions(parent_backend)


class _FakeExecCtx:
    member_id = None
    template_id = None


class _FakeInfo:
    def __init__(self, agent):
        self.id = "sub-1"
        self.agent = agent
        self.execution_context = _FakeExecCtx()
        self.model = ""
        self.allowed_tools = None
        self.bare = False
        self.parent_session_key = "parent-key"


class _FakeCfg:
    class agent:
        session_sharing = True

    @classmethod
    def load(cls):
        return cls


def _make_coordinator(parent_backend):
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    return RunEventCoordinator(_FakeManager(parent_backend))


def _patch_config(monkeypatch):
    """Make the gate's ``KiroCrewConfig.load()`` return a sharing-enabled config.

    The ``*_impl`` bodies resolve module-level names (``KiroCrewConfig``,
    ``logger``) from ``run.py``'s own globals, which ``bind_component_globals``
    populates only during real startup. Inject them directly so the gate reaches
    the new per-agent branch instead of tripping the ``except -> return False``
    guard on an unbound name.
    """
    import logging

    import kiro_crew.subagent_manager.run as run_mod

    monkeypatch.setattr(run_mod, "KiroCrewConfig", _FakeCfg, raising=False)
    monkeypatch.setattr(
        run_mod, "logger", logging.getLogger("test_per_agent_backend"), raising=False
    )


def test_child_backend_differs_forces_dedicated(agents_dir, monkeypatch):
    # Parent runs kiro (""); the child agent JSON selects claude. Sharing must
    # be refused so the dedicated path builds a claude process.
    _patch_config(monkeypatch)
    _write_agent(
        agents_dir, "claude-proposer", {"name": "claude-proposer", "acp_backend": "claude"}
    )
    coord = _make_coordinator(parent_backend="")  # parent = kiro
    info = _FakeInfo(agent="claude-proposer")
    assert coord._resolve_child_backend(info) == "claude"
    assert coord._resolve_parent_backend("parent-key") == ""
    assert coord._should_use_session_sharing_impl(info) is False


def test_child_backend_matches_parent_still_shares(agents_dir, monkeypatch):
    # Child selects claude AND parent is already claude: sharing stays enabled.
    _patch_config(monkeypatch)
    _write_agent(
        agents_dir, "claude-proposer", {"name": "claude-proposer", "acp_backend": "claude"}
    )
    coord = _make_coordinator(parent_backend="claude")
    info = _FakeInfo(agent="claude-proposer")
    assert coord._should_use_session_sharing_impl(info) is True


def test_child_without_override_shares(agents_dir, monkeypatch):
    # No per-agent override -> the mismatch guard is a no-op; sharing enabled.
    _patch_config(monkeypatch)
    _write_agent(agents_dir, "plain", {"name": "plain"})
    coord = _make_coordinator(parent_backend="")
    info = _FakeInfo(agent="plain")
    assert coord._resolve_child_backend(info) is None
    assert coord._should_use_session_sharing_impl(info) is True
