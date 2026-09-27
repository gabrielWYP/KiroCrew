"""A slot's ``SafetyOverride`` scoped grant reaches its spawn gate, its children and its badge.

An unattended app crew is trusted through ``slot._trust_scope`` plus a live scoped
grant, never through ``slot._trust``. Its own tool approvals honour that grant via
``chat_runner._slot_is_trusted``. The spawn prompt and every tool call of a spawned
child go through the gateway's ``_interactive_approval("subagent")`` callback, so
that callback must take the same verdict, audit which grant it rode, keep the
low-fidelity-child block, and never renew the grant.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.slot_projection import live_trust_scope
from kiro_crew.safety_override import safety_override
from kiro_crew.slack import gateway as gw

_SCOPE = "crew:slack-radar:autoapprove"
_SO = type(safety_override())


def _slot(*, trust: bool = False, scope: str = "") -> SimpleNamespace:
    """Exactly the two trust attributes named; a MagicMock would invent a scope."""
    return SimpleNamespace(key="crew-slot", _trust=trust, _trust_scope=scope, running=True)


def _orchestrator(slot: SimpleNamespace) -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={}):
        orch = gw.GatewayOrchestrator(cfg)
    orch.slack = None
    ds = MagicMock()
    ds._yolo = False
    ds._slots = {"crew-slot": slot}
    ds.request_approval = AsyncMock(return_value=False)
    ds.dashboard_user_ws_count.return_value = 1
    orch.dashboard_state = ds
    return orch


def _event(*, low_fidelity: bool = False) -> MagicMock:
    event = MagicMock()
    event.request_id = "spawn:abc123"
    event.title = "spawn_run(watch the channel)"
    event.tool_input = ""
    event.tool_purpose = ""
    event.child_low_fidelity = low_fidelity
    event.child_unconditional_grant_eligible = False
    return event


async def _decide(slot: SimpleNamespace, *, scope_live: bool, low_fidelity: bool = False):
    orch = _orchestrator(slot)
    callback = orch._interactive_approval("subagent", slot_resolver=lambda _rid: "crew-slot")
    log = MagicMock()
    with (
        patch.object(_SO, "is_scope_active", return_value=scope_live),
        patch.object(_SO, "is_active", return_value=False),
        patch.object(_SO, "renew_scoped") as renew,
        patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False),
        patch.object(gw, "sel") as sel_factory,
    ):
        sel_factory.return_value.log_api_access = log
        approved = await callback(_event(low_fidelity=low_fidelity), "")
    ops = [c.kwargs.get("operation") for c in log.call_args_list]
    return approved, ops, log, orch.dashboard_state.request_approval, renew


class TestSlotIsTrusted:
    def test_session_flag(self) -> None:
        assert chat_runner._slot_is_trusted(_slot(trust=True)) is True

    def test_live_scope(self) -> None:
        with patch.object(_SO, "is_scope_active", return_value=True):
            assert chat_runner._slot_is_trusted(_slot(scope=_SCOPE)) is True

    def test_lapsed_scope(self) -> None:
        with patch.object(_SO, "is_scope_active", return_value=False):
            assert chat_runner._slot_is_trusted(_slot(scope=_SCOPE)) is False

    def test_no_attributes(self) -> None:
        assert chat_runner._slot_is_trusted(SimpleNamespace()) is False


class TestSubagentApprovalHonoursScope:
    @pytest.mark.asyncio
    async def test_live_scope_auto_approves_with_its_own_audit(self) -> None:
        approved, ops, log, prompt, renew = await _decide(_slot(scope=_SCOPE), scope_live=True)
        assert approved is True
        prompt.assert_not_awaited()
        assert ops == ["subagent.trust_scope_auto_approve"]
        assert log.call_args.kwargs["resources"].startswith(f"scope:{_SCOPE} ")
        renew.assert_not_called()

    @pytest.mark.asyncio
    async def test_lapsed_scope_prompts(self) -> None:
        approved, ops, _log, prompt, _renew = await _decide(_slot(scope=_SCOPE), scope_live=False)
        assert approved is False
        prompt.assert_awaited_once()
        assert "subagent.scoped_trust_not_trusted" in ops
        assert "subagent.trust_scope_auto_approve" not in ops

    @pytest.mark.asyncio
    async def test_live_scope_still_blocks_a_low_fidelity_child(self) -> None:
        approved, ops, _log, prompt, _renew = await _decide(
            _slot(scope=_SCOPE), scope_live=True, low_fidelity=True
        )
        assert approved is False
        prompt.assert_awaited_once()
        assert "subagent.scoped_trust_blocked_low_fidelity_child" in ops

    @pytest.mark.asyncio
    async def test_session_flag_keeps_its_audit_name(self) -> None:
        approved, ops, _log, prompt, _renew = await _decide(_slot(trust=True), scope_live=False)
        assert approved is True
        prompt.assert_not_awaited()
        assert ops == ["subagent.scoped_trust_auto_approve"]


class TestProjection:
    def test_live_scope_is_projected(self) -> None:
        with patch.object(_SO, "scope_remaining_secs", return_value=120):
            assert live_trust_scope(_slot(scope=_SCOPE)) == _SCOPE

    def test_lapsed_scope_is_blank(self) -> None:
        with patch.object(_SO, "scope_remaining_secs", return_value=0):
            assert live_trust_scope(_slot(scope=_SCOPE)) == ""

    def test_no_scope_is_blank_without_a_lookup(self) -> None:
        with patch.object(_SO, "scope_remaining_secs") as remaining:
            assert live_trust_scope(_slot()) == ""
        remaining.assert_not_called()

    def test_projection_never_expires_the_grant(self) -> None:
        with (
            patch.object(_SO, "scope_remaining_secs", return_value=5),
            patch.object(_SO, "is_scope_active") as enforce,
        ):
            live_trust_scope(_slot(scope=_SCOPE))
        enforce.assert_not_called()

    def test_slot_dict_carries_the_field(self) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        slot._trust_scope = _SCOPE
        with patch.object(_SO, "scope_remaining_secs", return_value=60):
            d = slot.to_dict()
        assert d["trust_scope"] == _SCOPE
        assert d["trust"] is False
