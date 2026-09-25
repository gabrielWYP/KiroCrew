"""A listener sidecar must not outlive the listener it names.

The invariant: a ``run/gateway-<port>-<address>.secret`` file asserts that THIS
generation holds THAT address *now*. Clients read the set of them to answer "does
this gateway hold every family the name I am dialling resolves to?", and send the
credential only when the answer is yes -- so a sidecar that survives its listener
converts a refusal into a disclosure. A co-resident process binds the address the
dead listener freed, the client still reads coverage, and the credential goes to
the party that took the socket.

Two paths could leave that claim standing, and both are covered here:

* The second loopback family's listener was started and its handle DISCARDED, so
  nothing could observe its death or withdraw its sidecar. On Windows one failed
  ``accept()`` closes a LISTEN socket for good while the process lives on (see
  ``listener_guard``), which is exactly that death.
* Even a guarded listener has a REBIND WINDOW: from the moment it is confirmed
  dead until a rebind lands, nobody holds the address. A withdrawal that happens
  only after the rebind ladder gives up leaves the claim standing for the whole
  window, which is why the ordering here is pinned rather than the mere fact of a
  withdrawal.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.dashboard import server as dashboard_server
from kiro_crew.dashboard.listener_guard import LISTENER_LOST_EXIT_CODE, ListenerGuard
from kiro_crew.instances import run_marker


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)
    monkeypatch.setattr(run_marker, "_PUBLISHED_LISTENERS", {}, raising=True)
    return tmp_path


# ---------------------------------------------------------------------------
# run_marker: the single-address withdrawal
# ---------------------------------------------------------------------------


class TestWithdrawPublishedListener:
    def test_removes_the_file_and_the_in_memory_claim(self, home: Path) -> None:
        """Both halves. The in-memory set is what a later clear_marker deletes from."""
        path = run_marker.listener_secret_path(5476, "::1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("s3cret", encoding="utf-8")
        run_marker.note_published_listener(5476, "::1")
        assert run_marker.published_listeners(5476) == frozenset({"::1"})

        assert run_marker.withdraw_published_listener(5476, "::1") is True

        assert not path.exists()
        assert run_marker.published_listeners(5476) == frozenset()

    def test_leaves_the_other_addresses_of_the_same_port_alone(self, home: Path) -> None:
        """A port names a SET of listeners; withdrawing one must not touch its siblings."""
        for address in ("127.0.0.1", "::1"):
            p = run_marker.listener_secret_path(5476, address)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("s3cret", encoding="utf-8")
            run_marker.note_published_listener(5476, address)

        run_marker.withdraw_published_listener(5476, "::1")

        assert run_marker.listener_secret_path(5476, "127.0.0.1").exists()
        assert run_marker.published_listeners(5476) == frozenset({"127.0.0.1"})

    def test_refuses_an_address_this_process_never_published(self, home: Path) -> None:
        """Ownership, same rule as clear_marker: what cannot be proven is not deleted.

        Two gateways in one data home can hold the same port on different
        addresses. Unlinking an entry this process did not write would cost the
        live sibling every client that had already read it.
        """
        foreign = run_marker.listener_secret_path(5476, "::1")
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("someone-elses", encoding="utf-8")

        assert run_marker.withdraw_published_listener(5476, "::1") is False
        assert foreign.exists()
        assert foreign.read_text(encoding="utf-8") == "someone-elses"

    def test_a_missing_file_still_drops_the_claim(self, home: Path) -> None:
        """A sidecar this process can no longer vouch for stops being advertised."""
        run_marker.note_published_listener(5476, "::1")
        assert run_marker.withdraw_published_listener(5476, "::1") is True
        assert run_marker.published_listeners(5476) == frozenset()

    def test_an_empty_address_is_a_no_op(self, home: Path) -> None:
        assert run_marker.withdraw_published_listener(5476, "") is False


# ---------------------------------------------------------------------------
# ListenerGuard: the lifecycle hooks and the replaceable terminal action
# ---------------------------------------------------------------------------


async def _live(_request: web.Request) -> web.Response:
    return web.json_response({"alive": True})


class _Guarded:
    """A real aiohttp server on an ephemeral loopback port plus its guard."""

    def __init__(self, **guard_kwargs: Any) -> None:
        self.app = web.Application()
        self.app.router.add_get("/api/live", _live)
        self.runner = web.AppRunner(self.app)
        self.shutdown = asyncio.Event()
        self._guard_kwargs = guard_kwargs
        self.guard: ListenerGuard | None = None

    async def __aenter__(self) -> "_Guarded":
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.guard = ListenerGuard(self.runner, site, self.shutdown, **self._guard_kwargs)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.guard is not None:
            self.guard.stop()
        await self.runner.cleanup()


class TestRecoveryWindowOrdering:
    @pytest.mark.asyncio
    async def test_withdrawal_precedes_the_first_rebind_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering IS the fix, so the ordering is what is pinned.

        A guard that withdraws only after the rebind ladder finishes satisfies
        "a withdrawal happens" while leaving the false claim standing for the
        entire window in which the address is free. The event sequence has to be
        lost -> bind -> restored, with the withdrawal strictly BEFORE the first
        bind.
        """
        events: list[str] = []
        async with _Guarded(
            interval=3600,
            on_listener_lost=lambda: events.append("lost"),
            on_listener_restored=lambda: events.append("restored"),
        ) as served:
            guard = served.guard
            assert guard is not None
            old_site = guard.site
            monkeypatch.setattr(guard, "listener_open", lambda: guard.site is not old_site)

            original_new_site = guard._new_site

            async def _recording_new_site() -> Any:
                events.append("bind")
                return await original_new_site()

            monkeypatch.setattr(guard, "_new_site", _recording_new_site)

            assert await guard.check_now("test") is True

        assert events == ["lost", "bind", "restored"]
        assert events.index("lost") < events.index("bind")

    @pytest.mark.asyncio
    async def test_no_restore_when_every_rebind_attempt_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The address is re-advertised only once a bind actually lands."""
        events: list[str] = []
        async with _Guarded(
            interval=3600,
            max_attempts=2,
            backoff_base=0.0,
            max_backoff=0.0,
            on_listener_lost=lambda: events.append("lost"),
            on_listener_restored=lambda: events.append("restored"),
            on_give_up=lambda reason: events.append("gave-up"),
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            assert await guard.check_now("test") is False

        assert events == ["lost", "gave-up"]
        assert "restored" not in events


class TestReplaceableTerminalAction:
    @pytest.mark.asyncio
    async def test_injected_action_neither_exits_nor_signals_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A best-effort listener degrades; it must not kill a serving gateway."""
        reasons: list[str] = []
        async with _Guarded(
            interval=3600,
            max_attempts=1,
            backoff_base=0.0,
            on_give_up=reasons.append,
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            assert await guard.check_now("test") is False

            assert len(reasons) == 1
            assert guard.exit_code == 0
            assert served.shutdown.is_set() is False

    @pytest.mark.asyncio
    async def test_default_action_still_exits_non_zero_and_signals_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: the primary listener's behaviour is unchanged.

        Without this the test above passes for a guard that never escalates at
        all, which would be an outage dressed up as a degradation.
        """
        async with _Guarded(interval=3600, max_attempts=1, backoff_base=0.0) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            assert await guard.check_now("test") is False

            assert guard.exit_code == LISTENER_LOST_EXIT_CODE
            assert served.shutdown.is_set() is True

    @pytest.mark.asyncio
    async def test_injected_action_stops_the_guard_so_it_cannot_rebind_forever(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default path halted the loop via the shutdown event; this one cannot.

        Without stopping here the probe loop would rediscover the same dead
        listener every interval and rebind it for the life of the process.

        Asserting only that a later ``check_now`` reports "serving" would NOT pin
        this: a second, broader rule -- the ``_shutdown_event.is_set()`` check at
        the top of ``check_now`` and ``_recover`` -- returns exactly the same
        answer. So the assertions below are the pair that rule cannot satisfy: the
        shutdown event must still be CLEAR, and no further bind may be attempted
        anyway. A guard that escalated through the default path sets that event
        and fails the first half.
        """
        binds = 0
        async with _Guarded(
            interval=3600, max_attempts=1, backoff_base=0.0, on_give_up=lambda _r: None
        ) as served:
            guard = served.guard
            assert guard is not None
            monkeypatch.setattr(guard, "listener_open", lambda: False)

            async def _always_fails() -> Any:
                nonlocal binds
                binds += 1
                raise OSError("bind refused")

            monkeypatch.setattr(guard, "_new_site", _always_fails)

            await guard.check_now("test")
            assert binds == 1

            # Quiescent for the right reason: stopped, not shutting down.
            assert served.shutdown.is_set() is False
            assert await guard.check_now("again") is True
            assert binds == 1, "a stopped guard must not attempt another rebind"


# ---------------------------------------------------------------------------
# Two guards on one loop: chaining and the reverse-order detach
# ---------------------------------------------------------------------------


class TestTwoGuardsChain:
    @pytest.mark.asyncio
    async def test_reverse_order_detach_restores_the_original_handler(self) -> None:
        """Guards chain, so they must be detached in the reverse of the arming order.

        ``arm()`` captures whatever handler is installed and delegates to it, and
        each guard restores its neighbour only while it is still the installed
        handler. Detaching the outer one first therefore restores nothing and
        leaves the inner guard's handler on the loop for good.
        """
        loop = asyncio.get_running_loop()

        def _original(_loop: Any, _context: Any) -> None:
            return None

        loop.set_exception_handler(_original)
        try:
            async with _Guarded(interval=3600) as primary, _Guarded(interval=3600) as secondary:
                first, second = primary.guard, secondary.guard
                assert first is not None and second is not None
                first.arm()
                second.arm()
                assert loop.get_exception_handler() is not _original

                second.stop()
                first.stop()

                assert loop.get_exception_handler() is _original
        finally:
            loop.set_exception_handler(None)

    @pytest.mark.asyncio
    async def test_shipped_shutdown_hook_stops_the_secondary_first(self) -> None:
        """Pin the ORDER the shipped hook uses, not just that it stops both."""
        stopped: list[str] = []

        class _FakeGuard:
            def __init__(self, name: str) -> None:
                self._name = name

            def stop(self) -> None:
                stopped.append(self._name)

        class _FakeState:
            _listener_guard = _FakeGuard("primary")
            _secondary_listener_guard = _FakeGuard("secondary")

        app = web.Application()
        dashboard_server._register_listener_guard_shutdown(app, _FakeState())  # type: ignore[arg-type]
        for hook in app.on_cleanup:
            await hook(app)

        assert stopped == ["secondary", "primary"]


# ---------------------------------------------------------------------------
# The secondary helper must hand back something guardable
# ---------------------------------------------------------------------------


def _loopback_pair_available() -> bool:
    """Both loopback families bindable here, or the counterpart test proves nothing."""
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.bind((address, 0))
        except OSError:
            return False
    return True


class TestSecondaryLoopbackReturnsItsSite:
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _loopback_pair_available(), reason="both loopback families must be bindable"
    )
    async def test_the_second_listener_comes_back_with_a_live_site(self) -> None:
        """The address alone is unguardable: without the site nothing can observe its death.

        This is the precondition every candidate fix needed -- the helper started
        a ``web.SockSite`` and discarded it at return, so the sidecar it caused to
        be published could never be withdrawn.
        """
        app = web.Application()
        app.router.add_get("/api/live", _live)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            primary = web.TCPSite(runner, "127.0.0.1", 0)
            await primary.start()
            port = dashboard_server._resolved_bound_port(runner, 0)

            result = await dashboard_server._start_secondary_loopback_site(
                runner, port, "127.0.0.1"
            )

            assert result is not None
            assert result.address == "::1"
            assert result.site is not None
            sockets = getattr(getattr(result.site, "_server", None), "sockets", None)
            assert sockets, "the returned site must carry a live LISTEN socket"
            assert all(sock.fileno() != -1 for sock in sockets)
        finally:
            await runner.cleanup()
