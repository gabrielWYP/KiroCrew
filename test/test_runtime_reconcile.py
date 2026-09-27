"""Behaviour of the two-way runtime reconciler and of the lease gate on every sweep.

Two rules are under test, and they are opposites of each other.

The reconciler must ACT on a disagreement between the kernel and this gateway's
records, because a disagreement nobody resolves is a process that survives every
sweep and every restart. It must also be extremely reluctant to act in the one
direction that ends a process, because absence from a record is evidence that
something is unclaimed and not evidence that it is abandoned -- the unowned
population on a healthy host is mostly processes with good owners that no
*session* record describes.

The sweeps must REFUSE to signal a pid a session still holds a lease on. Each of
them already carries a shield, and the shield is a set gathered before the
candidate scan and an event-loop hop before the signal; the gate is asked about
one pid at the decision point, which is the only place the answer cannot already
be stale.

Every guard added by this change is named here with the test that goes red when
it is removed, so a later reader can verify the guard still earns its place:

* the spawn-marker condition -> ``test_a_process_without_our_marker_is_never_killed``
* the two-pass confirmation -> ``test_one_pass_only_counts_an_unowned_process``
* the age floor -> ``test_a_process_younger_than_the_floor_is_never_killed``
* the last-moment gate -> ``test_the_gate_still_refuses_after_every_other_condition``
* the kill budget -> ``test_one_pass_spends_a_bounded_number_of_kills``
* the unreadable-registry refusal -> ``test_an_unreadable_registry_refuses_the_whole_pass``
* the unsignalable-pid rule -> ``test_an_unsignalable_pid_is_not_a_dead_one``
* the recycle check -> ``test_a_record_whose_pid_now_names_a_stranger_counts_as_dead``
* its fail-closed answer -> ``test_an_unreadable_identity_is_not_a_stranger``
* the periodic pid sweep's gate -> ``test_the_periodic_pid_sweep_withholds_a_leased_pid``
* the untracked-MCP sweep's gate -> ``test_the_untracked_mcp_sweep_withholds_a_leased_pid``
* the scope reaper's gate -> ``test_the_scope_reaper_does_not_signal_a_leased_pid``
* the orphan reconcile's gate -> ``test_the_orphan_reconcile_withholds_a_leased_pid``
* the reset ladder's gate -> ``test_the_reset_ladder_withholds_the_kill_and_the_shared_child_sweep``
* the cron reaper's gate -> ``test_the_cron_reaper_reports_a_leased_runtime_instead_of_killing_it``

Every one of those pairings is executed, not asserted in prose: the harness named
in the pull request re-applies each mutation and requires the named test to fail.

Three positive controls sit beside them, because a refusal test passes for free
when the thing it refuses never happens: the spawn-marker read against a fixture
process table, the recycle seam against a registry file in the product's own
format, and the lease seam against the real ownership table.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import runtime_ownership as ro
from kiro_crew import runtime_reconcile as rr

# ── the reconciler core ───────────────────────────────────────────────────────


def _reconciler(
    *,
    kernel: set[int],
    recorded: set[int],
    alive: set[int] | None = None,
    recycled: set[int] | None = None,
    ours: set[int] | None = None,
    leased: set[int] | None = None,
    authorize: bool = True,
    age: float = 10_000.0,
    max_kills: int = rr.DEFAULT_MAX_KILLS,
    killed: list[int] | None = None,
    forgotten: list[int] | None = None,
    notified: list[int] | None = None,
) -> rr.RuntimeReconciler:
    """A reconciler over a fake kernel: no real processes, no real signals."""
    live = kernel if alive is None else alive
    return rr.RuntimeReconciler(
        slice_pids=lambda: set(kernel),
        recorded_pids=lambda: set(recorded),
        is_alive=lambda pid: pid in live,
        was_recycled=lambda pid: pid in (recycled or set()),
        is_ours=lambda pid: pid in (kernel if ours is None else ours),
        leases_on=lambda pid: 1 if pid in (leased or set()) else 0,
        authorize=lambda pid, reason: authorize,
        kill_tree=lambda pid: (killed if killed is not None else []).append(pid) or 1,
        forget=lambda pid: (forgotten if forgotten is not None else []).append(pid) is None,
        notify_dead=lambda pid: (notified if notified is not None else []).append(pid),
        age_secs=lambda pid: age,
        max_kills=max_kills,
    )


def test_a_live_process_no_record_claims_is_counted() -> None:
    """The reading is the point even when nothing is killed: a leak an operator
    can see is a leak that gets fixed."""
    killed: list[int] = []
    reading = _reconciler(kernel={100, 200}, recorded={100}, killed=killed).run_once()
    assert reading.unowned_alive == 1
    assert reading.owned_alive == 1
    assert killed == [], "nothing dies on the pass that first notices it"


def test_one_pass_only_counts_an_unowned_process() -> None:
    """MUTATION TARGET: the two-pass confirmation.

    A record is published AFTER the process it describes exists, so every spawn
    has a window in which it is unowned. Killing on one sighting kills inside
    that window.
    """
    killed: list[int] = []
    rec = _reconciler(kernel={100, 200}, recorded={100}, killed=killed)
    first = rec.run_once()
    assert first.killed == 0
    assert ("first pass unowned",) == tuple(why for _pid, why in first.withheld)
    second = rec.run_once()
    assert second.killed == 1 and killed == [200]


def test_a_process_without_our_marker_is_never_killed() -> None:
    """MUTATION TARGET: the spawn-marker condition.

    The marker is read out of the kernel's exec-time copy, which a same-uid
    process cannot forge. Without it the reconciler would end a stranger's
    process that merely happened to be inside the slice.
    """
    killed: list[int] = []
    rec = _reconciler(kernel={100, 200}, recorded={100}, ours=set(), killed=killed)
    rec.run_once()
    reading = rec.run_once()
    assert reading.killed == 0 and killed == []
    assert ("no spawn marker",) == tuple(why for _pid, why in reading.withheld)


def test_a_process_younger_than_the_floor_is_never_killed() -> None:
    """MUTATION TARGET: the age floor. The same registration window, seen from
    the process's side rather than from the record's."""
    killed: list[int] = []
    rec = _reconciler(kernel={100, 200}, recorded={100}, age=1.0, killed=killed)
    rec.run_once()
    reading = rec.run_once()
    assert reading.killed == 0 and killed == []
    assert ("younger than the age floor",) == tuple(why for _pid, why in reading.withheld)


def test_the_gate_still_refuses_after_every_other_condition() -> None:
    """MUTATION TARGET: the last-moment ownership gate.

    Every condition above is about the process. This one is about who is using
    it, and it is asked last because a lease can be taken between the scan and
    the signal.
    """
    killed: list[int] = []
    rec = _reconciler(kernel={100, 200}, recorded={100}, authorize=False, killed=killed)
    rec.run_once()
    reading = rec.run_once()
    assert reading.killed == 0 and killed == []
    assert ("refused by the ownership gate",) == tuple(why for _pid, why in reading.withheld)


def test_a_leased_pid_is_never_unowned() -> None:
    """A lease IS a record. A pid with one outstanding is claimed, whatever the
    session map and the backend pidfile happen to say."""
    reading = _reconciler(kernel={100, 200}, recorded=set(), leased={100, 200}).run_once()
    assert reading.unowned_alive == 0


def test_one_pass_spends_a_bounded_number_of_kills() -> None:
    """MUTATION TARGET: the kill budget.

    A reconciler that has misjudged a whole population should be wrong slowly
    enough for the reading to be noticed before the population is gone.
    """
    kernel = set(range(100, 120))
    killed: list[int] = []
    rec = _reconciler(kernel=kernel, recorded=set(), max_kills=3, killed=killed)
    rec.run_once()
    reading = rec.run_once()
    assert reading.killed == 3 and len(killed) == 3
    assert "kill budget spent" in {why for _pid, why in reading.withheld}


def test_a_record_naming_a_dead_process_is_retracted_and_its_holder_told() -> None:
    """The other direction. Forgetting costs nothing -- the process is already
    gone -- and the holder learns why instead of waiting out a timeout."""
    forgotten: list[int] = []
    notified: list[int] = []
    reading = _reconciler(
        kernel=set(),
        recorded={100, 200},
        alive={100},
        forgotten=forgotten,
        notified=notified,
    ).run_once()
    assert reading.owned_dead == 1 and reading.owned_alive == 1
    assert forgotten == [200] and notified == [200]


def test_a_record_whose_pid_now_names_a_stranger_counts_as_dead() -> None:
    """MUTATION TARGET: the recycle check in the dead direction.

    A live pid whose start identity differs from the recorded one is not our
    process. Counting it healthy is what leaves a sweep signalling by pid aimed
    at somebody else, so it joins the dead population, its record is retracted,
    and the stranger itself is never signalled.
    """
    killed: list[int] = []
    forgotten: list[int] = []
    notified: list[int] = []
    reading = _reconciler(
        kernel={200},
        recorded={200},
        alive={200},
        recycled={200},
        killed=killed,
        forgotten=forgotten,
        notified=notified,
    ).run_once()
    assert reading.owned_dead == 1 and reading.owned_alive == 0
    assert forgotten == [200] and notified == [200]
    assert killed == [], "the current holder of a recycled pid is not ours to end"


def test_a_recycled_pid_is_never_also_counted_as_unowned() -> None:
    """One process, one population. A recycled pid is in a record, so it is a
    disagreement about that record and never a second finding in the kernel
    direction -- where it would be eligible for a kill."""
    killed: list[int] = []
    rec = _reconciler(kernel={200}, recorded={200}, alive={200}, recycled={200}, killed=killed)
    rec.run_once()
    reading = rec.run_once()
    assert reading.unowned_alive == 0
    assert killed == []


def test_an_unreadable_identity_is_not_a_stranger() -> None:
    """MUTATION TARGET: the recycle check's fail-closed answer.

    The start token is subtractive only: an identity that cannot be read on
    either side is an unknown, never a mismatch. A live runtime called a stranger
    loses the record that is the only thing able to find it again.
    """
    forgotten: list[int] = []

    def raising_check(pid: int) -> bool:
        raise OSError("cannot read the identity")

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {100},
        is_alive=lambda pid: True,
        was_recycled=raising_check,
        kill_tree=lambda pid: 0,
        forget=lambda pid: forgotten.append(pid) is None,
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.owned_dead == 0 and reading.owned_alive == 1
    assert forgotten == []


def test_an_unsignalable_pid_is_not_a_dead_one() -> None:
    """MUTATION TARGET: the liveness probe's tri-state handling.

    An unreadable probe is an unknown process. Retracting a record on that
    answer is how a live runtime loses the only thing that can find it again.
    """
    forgotten: list[int] = []

    def raising_probe(pid: int) -> bool:
        raise PermissionError("cannot probe")

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {100},
        is_alive=raising_probe,
        kill_tree=lambda pid: 0,
        forget=lambda pid: forgotten.append(pid) is None,
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.owned_dead == 0 and forgotten == []


def test_an_unreadable_registry_refuses_the_whole_pass() -> None:
    """MUTATION TARGET: the registry-read refusal.

    A registry that cannot be read makes EVERY live process look unowned. That
    single input is the one that turns a pass into a massacre, so the pass is
    abandoned rather than half-applied.
    """
    killed: list[int] = []

    def no_registry() -> set[int]:
        raise OSError("gatewayd pidfile is gone")

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {100, 200, 300},
        recorded_pids=no_registry,
        is_alive=lambda pid: True,
        kill_tree=lambda pid: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.supported is False and "registry" in reading.reason
    assert reading.unowned_alive == 0 and killed == []


def test_an_unreadable_slice_refuses_the_pass() -> None:
    def no_slice() -> set[int]:
        raise OSError("cgroup is not delegated")

    rec = rr.RuntimeReconciler(
        slice_pids=no_slice,
        recorded_pids=lambda: {100},
        is_alive=lambda pid: True,
        kill_tree=lambda pid: 0,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.supported is False and "slice" in reading.reason


def test_the_reading_publishes_the_two_liveness_sli_names() -> None:
    """The counts are consumed by the liveness SLI under these exact names."""
    fields = _reconciler(kernel={100, 200}, recorded={100}).run_once().as_counter_fields()
    assert fields["unowned_alive"] == 1
    assert "owned_dead" in fields


def test_the_gateway_never_reports_itself_as_unowned() -> None:
    """The reconciler runs inside the very slice it reads."""
    mine = os.getpid()
    reading = _reconciler(kernel={mine, 1}, recorded=set()).run_once()
    assert reading.unowned_alive == 0, "our own pid and init are never candidates"


def test_the_marker_read_answers_from_a_fixture_process_table(tmp_path: Path) -> None:
    """POSITIVE CONTROL for the marker: without this, ``is_ours`` returning False
    everywhere would satisfy every refusal test above and mean nothing."""
    proc = tmp_path / "4242"
    proc.mkdir()
    (proc / "environ").write_bytes(b"PATH=/usr/bin\0KIROCREW_SPAWNED=1\0")
    assert rr.process_is_ours(4242, proc_root=tmp_path) is True

    other = tmp_path / "4243"
    other.mkdir()
    (other / "environ").write_bytes(b"PATH=/usr/bin\0")
    assert rr.process_is_ours(4243, proc_root=tmp_path) is False


def test_the_wiring_reads_a_recycled_identity_out_of_the_real_registry_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE CONTROL for the recycle seam: without this, ``was_recycled``
    never firing in production would satisfy the unit cases above and mean
    nothing.

    Writes the session file the product writes, in the product's own
    ``<gw>:<pid>:<token>`` form, then answers the live identity lookup with a
    different token for one pid and the recorded token for the other.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    gw = os.getpid()
    (home / "kiro_session_pids.txt").write_text(
        f"{gw}:5001:TOKEN-AS-RECORDED\n{gw}:5002:TOKEN-AS-RECORDED\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        session_pid,
        "_pid_start_token",
        lambda pid: "TOKEN-AS-RECORDED" if pid == 5001 else "A-DIFFERENT-PROCESS",
    )
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())

    forgotten: list[int] = []
    monkeypatch.setattr(session_pid, "_untrack_session_pid", lambda pid: forgotten.append(pid))
    monkeypatch.setattr("kiro_crew.session_scope_reap.instance_slice_pids", lambda: {5001, 5002})
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_ALIVE
    )

    reconciler = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    reading = reconciler.run_once()

    assert reading.owned_dead == 1, "the pid whose live identity differs is the dead one"
    assert reading.owned_alive == 1
    assert forgotten == [5002]
    assert reading.unowned_alive == 0, "both pids are in a record, so neither is unowned"


@pytest.mark.asyncio
async def test_the_lease_seam_reads_the_real_ownership_table() -> None:
    """POSITIVE CONTROL for the lease seam: without this, the production default
    answering 0 for everything would satisfy every refusal test above and mean
    nothing.

    The reconciler starts from the kernel's list of process ids, so a pid is all
    it has and the table's pid accessor is the whole answer. Asked here through
    the same public surface the production default uses.
    """
    ro._reset_for_tests()
    assert rr._leases_on_pid(4321) == 0

    holder = _LeaseHolder(4321)
    await holder.take()
    try:
        assert rr._leases_on_pid(4321) == 1, "a held lease is visible to the seam"
    finally:
        await holder.give_back()

    assert rr._leases_on_pid(4321) == 0, "releasing it is visible too"


def test_a_kill_that_signalled_nothing_is_not_counted_as_one() -> None:
    """MUTATION TARGET: reading the tree-kill seam's count.

    The seam re-applies its own recycle guard and signals nothing for a pid that
    does not look like a managed agent process -- which is most of what an unowned
    reading is made of. Counting an unsignalled call as a kill would spend the
    whole per-pass budget on the same lowest pids every pass, forever, while the
    reading reported kills and nothing changed.
    """
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {100, 200, 300},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        is_ours=lambda pid: True,
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid: 0,  # the guard inside refused: nothing signalled
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()
    reading = rec.run_once()
    assert reading.killed == 0, "an unsignalled call is not a kill"
    assert ["kill signalled nothing"] * 3 == [why for _pid, why in reading.withheld]


def test_the_holders_of_dead_runtimes_are_told_once_not_every_pass() -> None:
    """MUTATION TARGET: the notify-once memory.

    Retraction does not always remove the pid from every record -- one held in the
    manager's own live union is not in the file the untracker rewrites -- so the
    same disagreement is rediscovered on every pass. The COUNT must stay a fresh
    reading, but the notification reaches the user's chat, and one per pass per
    cleanup interval would append to it for as long as the gateway runs.
    """
    notified: list[int] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {100},
        is_alive=lambda pid: False,
        kill_tree=lambda pid: 0,
        forget=lambda pid: True,  # the rewrite lands; the pid stays in another record
        notify_dead=notified.append,
    )
    first = rec.run_once()
    second = rec.run_once()
    assert notified == [100], f"told once, not once per pass; got {notified}"
    assert first.owned_dead == 1 and second.owned_dead == 1, "the count stays a fresh reading"


def test_an_absent_backend_pidfile_is_an_empty_set_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: absence and unreadability are different answers.

    No broker runs at all under the default empty stub configuration, gatewayd
    unlinks the file on a clean shutdown, and it appears only a heartbeat after
    start. Treating any of those as a refusal leaves both directions and the
    liveness reading permanently inert behind a single debug line -- the quietest
    possible way for this module to do nothing at all.
    """
    missing = tmp_path / "gateway.sock"
    monkeypatch.setattr("kiro_crew.runtime_reconcile.configured_socket_path", lambda: str(missing))
    assert rr._mcp_backend_pids() == set(), "an absent pidfile means nothing is hosted"

    # A file that EXISTS and cannot be read is still a refusal: presenting every
    # live backend as unowned is the one input that makes a pass dangerous.
    unreadable = tmp_path / "gateway.sock.backends"
    unreadable.mkdir()
    with pytest.raises(OSError):
        rr._mcp_backend_pids()


def test_a_recycled_pid_does_not_inherit_the_previous_passs_confirmation() -> None:
    """MUTATION TARGET: the two-pass memory is keyed on identity, not just number.

    Keyed on the number alone, a candidate that exits between passes hands its
    confirmation to whatever process the kernel gives the number to next -- so the
    replacement is eligible on its first sighting, which is exactly what the
    two-pass rule exists to prevent.
    """
    killed: list[int] = []
    identities = {200: "as-classified"}

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=lambda pid: identities[pid],
        is_ours=lambda pid: True,
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()  # classified once; nothing dies on first sighting
    identities[200] = "a-different-process"  # exited, and the kernel reused 200
    reading = rec.run_once()

    assert killed == [], "the replacement is on its own first pass, not the victim's second"
    assert ("first pass unowned",) == tuple(why for _pid, why in reading.withheld)

    # And once the replacement has its OWN two passes, it is eligible.
    third = rec.run_once()
    assert killed == [200] and third.killed == 1


def test_an_identity_that_changes_before_the_signal_withholds_the_kill() -> None:
    """MUTATION TARGET: the identity re-check in the instant before the signal.

    Every check before it -- the two-pass memory, the marker, the age floor, the
    gate -- inspected a pid NUMBER, and the kernel can hand that number to another
    process after any of them. The last-instant re-read is what keeps the signal
    aimed at the process this pass actually classified.

    Simulated where it really happens: WITHIN one pass, between classification and
    the signal, by answering the identity read differently the second time.
    """
    killed: list[int] = []
    reads: dict[int, int] = {200: 0}

    def identity_of(pid: int) -> str | None:
        reads[pid] += 1
        # Stable while the two-pass memory is being built, then changed on the
        # read that happens immediately before the signal.
        return "as-classified" if reads[pid] <= 2 else "a-different-process"

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=identity_of,
        is_ours=lambda pid: True,
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()
    reading = rec.run_once()

    assert killed == [], "a pid that is no longer the classified process is not signalled"
    assert ("process identity changed since classification",) == tuple(
        why for _pid, why in reading.withheld
    )


def test_an_unreadable_identity_withholds_the_kill() -> None:
    """The fail-closed half of the same check: a process this pass cannot identify
    is one it cannot claim to have inspected, so the kill waits for a pass that
    can. Cheap -- the next pass retries."""
    killed: list[int] = []

    def no_identity(pid: int) -> str | None:
        raise OSError("cannot read the identity")

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=no_identity,
        is_ours=lambda pid: True,
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()
    reading = rec.run_once()
    assert killed == []
    assert ("process identity changed since classification",) == tuple(
        why for _pid, why in reading.withheld
    )


def test_an_identity_unreadable_at_the_last_moment_withholds_the_kill() -> None:
    """MUTATION TARGET: the fail-closed answer in the last-instant re-read itself.

    Distinct from the case where the identity was never captured: here
    classification succeeded and the re-read in the instant before the signal is
    what fails. A process whose identity cannot be confirmed at the moment of the
    signal is one this pass cannot claim to be signalling, so the kill waits.
    """
    killed: list[int] = []
    reads: dict[int, int] = {200: 0}

    def identity_of(pid: int) -> str | None:
        reads[pid] += 1
        if reads[pid] > 2:  # the read immediately before the signal
            raise OSError("cannot read the identity now")
        return "as-classified"

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=identity_of,
        is_ours=lambda pid: True,
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()
    reading = rec.run_once()
    assert killed == []
    assert ("process identity changed since classification",) == tuple(
        why for _pid, why in reading.withheld
    )


def test_process_age_comes_from_the_process_start_not_the_procfs_inode() -> None:
    """MUTATION TARGET: which clock the age floor reads.

    ``/proc/<pid>``'s own ``st_ctime`` is assigned when procfs instantiates the
    inode, which can be long after the process started. An age taken from it reads
    every candidate as younger than the floor, so the kill arm never reclaims
    anything and the whole population grows unchecked while the reading looks
    healthy.

    Pinned against the repository's own helper rather than a recomputation of it,
    and against this live process, whose age is genuinely non-zero.
    """
    from kiro_crew.session_pid import _pid_age_seconds

    mine = os.getpid()
    expected = _pid_age_seconds(mine)
    assert expected is not None, "this process's own age must be readable"
    measured = rr.process_age_secs(mine)
    assert measured > 0.0, "a live process is not zero seconds old"
    # Tight on purpose: the two readings are of the same clock microseconds apart,
    # so any real disagreement is the wrong clock. A loose tolerance would accept a
    # constant, because the pytest process is itself only seconds old.
    assert abs(measured - expected) < 0.5, f"age {measured} disagrees with the helper {expected}"

    # Fail-closed: an unreadable pid is too young to touch, never old enough.
    assert rr.process_age_secs(2**31 - 1) == 0.0


def test_a_retraction_the_untracker_refused_is_not_counted_as_one() -> None:
    """MUTATION TARGET: reading the untracker's answer.

    ``_untrack_session_pid`` returns False when its rewrite of the tracking file
    could not land, which leaves the stale entry in place. Counting that as a
    retraction reports work that did not happen; the count of DEAD records stays
    honest either way, because the disagreement is still there and is re-detected
    on the next pass.
    """
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {100},
        is_alive=lambda pid: False,
        kill_tree=lambda pid: 0,
        forget=lambda pid: False,  # the rewrite was refused
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.owned_dead == 1, "the record is still stale, and the reading says so"
    assert reading.forgotten == 0, "but nothing was retracted, so nothing is counted"


def test_every_kill_decision_is_audited(tmp_path: Path) -> None:
    """MUTATION TARGET: the SEL audit on each outcome.

    Every other reap path in this gateway audits the processes it ends. A kill arm
    that did not would be the one place a process is signalled with no record of
    who decided it. The refusal is audited too, because "we decided not to" is what
    an operator needs when a leak reading stays non-zero.
    """
    events: list[tuple[int, str, str]] = []

    def audit(pid: int, outcome: str, reason: str) -> None:
        events.append((pid, outcome, reason))

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200, 300},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=lambda pid: "stable",
        is_ours=lambda pid: pid == 200,  # 300 lacks the marker and is withheld
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        audit=audit,
        kill_tree=lambda pid: 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()
    events.clear()
    rec.run_once()

    by_outcome = {outcome for _pid, outcome, _why in events}
    assert by_outcome == {"killed", "refused"}, f"both outcomes are audited; got {by_outcome}"
    assert (200, "killed") in [(p, o) for p, o, _ in events]
    assert (300, "refused") in [(p, o) for p, o, _ in events]


def test_the_audit_seam_reaches_sel_and_cannot_break_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSITIVE CONTROL for the audit, plus its fail-open rule.

    Without the control, an audit function that silently did nothing would satisfy
    the test above. And an audit that raised must not stop a reconciliation pass:
    losing one record is bad, losing the sweep is worse.
    """
    logged: list[dict[str, Any]] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs: Any) -> None:
            logged.append(kwargs)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    rr._sel_reconcile_kill(4242, "killed", "because")
    assert logged and logged[0]["tool_name"] == "runtime_reconcile"
    assert logged[0]["tool_kind"] == "process_kill"
    assert logged[0]["outcome"] == "killed"
    assert "4242" in logged[0]["resources"]

    def boom() -> Any:
        raise RuntimeError("sel is down")

    monkeypatch.setattr("kiro_crew.sel.sel", boom)
    rr._sel_reconcile_kill(4242, "killed", "because")  # must not raise


def test_a_withheld_pid_is_audited_on_a_reason_change_not_every_pass() -> None:
    """MUTATION TARGET: the reason-transition memory on the audit.

    Most of the unowned population is withheld permanently -- every MCP server and
    sandbox helper in the slice sits at the same reason forever -- so one event per
    pid per pass writes thousands of identical rows a day into a log with a finite
    rotation ceiling, evicting the history an operator needs. A transition is the
    event; a steady state is not.
    """
    events: list[tuple[int, str]] = []
    ours = {200}

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=lambda pid: "stable",
        is_ours=lambda pid: pid in ours,
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: False,  # always withheld, same reason
        audit=lambda pid, outcome, why: events.append((pid, why)),
        kill_tree=lambda pid: 0,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()  # first pass: reason is "first pass unowned"
    first = list(events)
    rec.run_once()  # now "refused by the ownership gate" -- a transition
    rec.run_once()  # same reason again -- no event
    rec.run_once()  # and again
    transitions = [why for _pid, why in events]

    assert first, "the first sighting is itself a transition and is audited"
    assert transitions.count("refused by the ownership gate") == 1, (
        "the steady state is audited once, not once per pass; " f"got {transitions}"
    )


def test_the_gate_is_not_asked_for_a_pid_the_earlier_checks_withhold() -> None:
    """MUTATION TARGET: asking the gate LAST.

    The gate's allow path writes the kill attribution, so asking it before the
    remaining checks records a kill of every pid those checks then withhold. The
    identity re-check is the one that runs before it.
    """
    asked: list[int] = []
    reads: dict[int, int] = {200: 0}

    def identity_of(pid: int) -> str | None:
        reads[pid] += 1
        return "as-classified" if reads[pid] <= 2 else "a-different-process"

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=identity_of,
        is_ours=lambda pid: True,
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: asked.append(pid) is None,
        kill_tree=lambda pid: 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()
    rec.run_once()
    assert asked == [], "a pid withheld on identity never reaches the attributing gate"


def test_the_wiring_claims_pids_tracked_by_any_gateway_on_this_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: membership read at the same scope as the kernel question.

    The agent slice is named from a hash of the data home, so a SECOND process on
    that home -- a ``kirocrew chat`` doing agent work in-process, or this gateway's
    own namespace-sandbox children whose tracked pid is the launcher parent -- puts
    live runtimes in the very slice this reconciler reads. Their records are filed
    under a different gateway pid, or in the descendant pid file, and the lease
    table is per-process memory that cannot see them at all.

    Asked only for THIS process's session entries, every one of them is unclaimed
    by construction, passes the inherited marker test, ages past the floor, and is
    signalled. So membership comes from both pid files across every gateway pid.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    mine, other = os.getpid(), os.getpid() + 1
    # 5101 is ours; 5202 belongs to another gateway pid on the same home; 5303 is a
    # sandbox child, recorded only as a descendant in the other file.
    (home / "kiro_session_pids.txt").write_text(
        f"{mine}:5101:TOKEN-A\n{other}:5202:TOKEN-B\n", encoding="utf-8"
    )
    (home / "kiro_pids.txt").write_text(f"5303:{mine}\n", encoding="utf-8")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    # Patched on THIS module: the enumerator is a hoisted module-scope import here,
    # so patching its source module would leave this reference untouched and the
    # reconciler would read the host's real slice.
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: {5101, 5202, 5303})
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_ALIVE
    )
    monkeypatch.setattr(session_pid, "_pid_start_token", lambda pid: None)

    killed: list[int] = []
    monkeypatch.setattr(session_pid, "_kill_pid_tree", lambda pid: (killed.append(pid) or 1, True))
    monkeypatch.setattr(session_pid, "_untrack_session_pid", lambda pid: True)

    reconciler = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    reading = reconciler.run_once()
    reconciler.run_once()

    assert reading.unowned_alive == 0, (
        "a pid any record on this data home claims is owned, whichever gateway "
        "filed it and whichever file it is in"
    )
    assert killed == [], "and nothing is signalled"


def test_an_incomplete_tracked_pid_snapshot_refuses_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: the completeness requirement on membership.

    A partial read of the tracking files is the one input that makes a live runtime
    look unowned, so it refuses the pass rather than authorizing a kill on
    incomplete membership -- the same requirement the scope reaper imposes on every
    kill-authorizing caller.
    """
    from kiro_crew import session_pid

    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(session_pid, "_session_pid_entry_index", lambda gw: {})
    monkeypatch.setattr(session_pid, "_read_tracked_agent_pids", lambda: ({5101}, False))
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: {5101, 9999})

    reconciler = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    reading = reconciler.run_once()
    assert reading.supported is False
    assert "registry" in reading.reason, reading.reason


def test_a_foreign_gateways_dead_record_is_counted_but_not_claimed_retracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: carrying each record's OWNER, not just its identity.

    Membership spans every gateway's rows on this data home, so a row filed by a
    concurrent CLI or a predecessor gateway reaches the dead direction. The
    untracker matches on the CALLING process's own prefix and cannot remove such a
    row -- and an unchanged rewrite still returns True, so calling it would report a
    retraction that never happened, every pass, forever.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    mine, foreign = os.getpid(), os.getpid() + 1
    (home / "kiro_session_pids.txt").write_text(
        f"{mine}:5001:TOK-A\n{foreign}:5002:TOK-B\n", encoding="utf-8"
    )
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_DEAD
    )
    untracked: list[int] = []
    monkeypatch.setattr(
        session_pid, "_untrack_session_pid", lambda pid: untracked.append(pid) is None
    )

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    assert reading.owned_dead == 2, "both dead rows are real and both are counted"
    assert reading.forgotten == 1, "only the row this gateway can remove is claimed retracted"
    assert untracked == [5001], f"the foreign row is never handed to the untracker; {untracked}"


def test_the_entry_reader_carries_every_gateways_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE CONTROL for the reader: without it, a reader that silently returned
    nothing would satisfy the test above by making both rows invisible."""
    from kiro_crew import session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    (home / "kiro_session_pids.txt").write_text(
        "111:5001:TOK-A\n222:5002\ngarbage\n333:5003:TOK-C\n", encoding="utf-8"
    )
    owners = rr._session_pid_entry_owners()
    assert owners == {5001: (111, "TOK-A"), 5002: (222, None), 5003: (333, "TOK-C")}, owners


# ── the sweep gates ──────────────────────────────────────────────────────────


class _Session:
    def __init__(self, pid: int | None = None) -> None:
        self.provider = object()
        self.semaphore = asyncio.BoundedSemaphore(1)
        self.last_used = 0.0
        self._pid = pid


class _Owner:
    """The slice of ``SessionManager`` the cleanup service reaches through."""

    def __init__(self) -> None:
        self._cfg = _Cfg()
        self._sessions: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._draining_bg_runtimes: list[Any] = []
        self._bg_runtime_lock = asyncio.Lock()
        self.on_session_expire = None
        self.on_stuck_turn = None
        self.recycled: list[tuple[str, str]] = []

    def get_pid(self, key: str) -> int | None:
        return getattr(self._sessions.get(key), "_pid", None)

    def _pool_pids(self) -> set[int]:
        return set()

    def _in_flight_pids(self) -> set[int]:
        return set()

    def _companion_runtime_pids(self) -> set[int]:
        return set()

    async def _fire_recycle_callback(self, key: str, *, reason: str) -> None:
        self.recycled.append((key, reason))

    async def _cleanup_loop(self) -> None:  # pragma: no cover - not driven here
        return None

    async def _expire_idle(self, timeout_secs: int) -> None:  # pragma: no cover
        return None

    async def _reap_drained_bg_runtimes_locked(self) -> None:  # pragma: no cover
        return None

    async def _reap_idle_stale_bg_runtime(self) -> bool:  # pragma: no cover
        return False

    async def reset(self, key: str, **kwargs: Any) -> bool:  # pragma: no cover
        return False


class _SessionCfg:
    timeout_secs = 0
    watchdog_rss_max_mb = 0


class _Cfg:
    session = _SessionCfg()


def _cleanup(
    *,
    candidates: list[int],
    mcp_candidates: list[int] | None = None,
    active: set[int] | None = None,
) -> tuple[Any, dict[str, list[int]]]:
    """A ``SessionCleanup`` whose sweeps report *candidates* and record kills."""
    from kiro_crew.session_cleanup import CleanupDeps, CleanupState, SessionCleanup
    from kiro_crew.watchdog import SessionWatchdog

    recorded: dict[str, list[int]] = {"pid_kills": [], "mcp_kills": []}
    executor = ThreadPoolExecutor(max_workers=1)

    class _Shutdown:
        def is_set(self) -> bool:
            return True

        def wait(self) -> Any:
            fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
            return fut

    def kill_confirmed(gateway_pid: int, confirmed: list[int], dead: set[str]) -> int:
        recorded["pid_kills"].extend(confirmed)
        return len(confirmed)

    def kill_mcps(pids: list[int]) -> int:
        recorded["mcp_kills"].extend(pids)
        return len(pids)

    deps = CleanupDeps(
        logger=logging.getLogger("test.w4"),
        get_shutdown_signal=_Shutdown,
        get_maintenance_executor=lambda: executor,
        get_subprocess_executor=lambda: executor,
        cleanup_orphaned_mcp_servers=lambda: 0,
        cleanup_orphaned_session_roots=lambda: 0,
        cleanup_stale_sandbox_profiles=lambda: 0,
        prune_session_pid_mappings=lambda: 0,
        prune_member_pid_bindings=lambda: 0,
        prune_pycache=lambda: (0, 0),
        collect_active_pids=lambda sessions: (set(active or set()), True),
        periodic_pid_sweep=lambda gw, pids: (set(), list(candidates)),
        kill_confirmed_and_writeback=kill_confirmed,
        find_orphan_mcp_candidates=lambda pids: list(
            candidates if mcp_candidates is None else mcp_candidates
        ),
        kill_orphan_mcps=kill_mcps,
        reap_agent_scopes=lambda pids: None,
        build_child_map=dict,
        rss_mb_from_tree=lambda pid, child_map: 0,
        get_session_rss_mb=lambda pid: 0,
        is_windows=lambda: False,
        getpid=lambda: 1,
        monotonic=lambda: 0.0,
        stats_factory=lambda: _Stats(),
        sel_factory=lambda: _Sel(),
        provider_has_active_turn=lambda provider: False,
        emit_counter=lambda event, dims: None,
        get_persistent_keys=frozenset,
        get_channel_prefix=lambda: "channel:",
        get_stuck_turn_report_secs=lambda: 1e9,
        get_pycache_gc_interval_secs=lambda: 1e9,
        get_session_idle_expired_event=lambda: "idle",
    )
    state = CleanupState(watchdog=SessionWatchdog([]))
    return SessionCleanup(_Owner(), deps, state=state), recorded


class _Stats:
    def inc_session_cleaned(self) -> None:
        return None


class _Sel:
    def log_api_access(self, **kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_the_periodic_pid_sweep_withholds_a_leased_pid() -> None:
    """MUTATION TARGET: ``_kill_authorized`` in ``_sweep_periodic_pids``.

    The shield cannot cover this: it is gathered before the candidate scan and an
    event-loop hop before the kill, so a lease taken in between is invisible to
    it and visible here.
    """
    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[4242, 4343])
    holder = _LeaseHolder(4242)
    await holder.take()
    try:
        await service._sweep_periodic_pids()
    finally:
        await holder.give_back()
    assert recorded["pid_kills"] == [4343], "the leased pid must not reach the killer"


@pytest.mark.asyncio
async def test_the_periodic_pid_sweep_still_kills_an_unleased_orphan() -> None:
    """CONTROL: without this, a gate that refused everything would satisfy the
    test above and stop the sweep working at all."""
    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[4242, 4343])
    await service._sweep_periodic_pids()
    assert sorted(recorded["pid_kills"]) == [4242, 4343]


@pytest.mark.asyncio
async def test_the_untracked_mcp_sweep_withholds_a_leased_pid() -> None:
    """MUTATION TARGET: ``_kill_authorized`` in ``_sweep_untracked_mcps``."""
    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[], mcp_candidates=[5151, 5252])
    holder = _LeaseHolder(5151)
    await holder.take()
    try:
        await service._sweep_untracked_mcps()
    finally:
        await holder.give_back()
    assert recorded["mcp_kills"] == [5252]


@pytest.mark.asyncio
async def test_an_unanswerable_gate_withholds_the_pid() -> None:
    """MUTATION TARGET: the fail-closed answer inside ``_kill_authorized``.

    A gate that raises is a refusal: deferring a housekeeping kill one tick costs
    nothing that killing a live runtime would not cost more.

    Two candidates, and the gate raises for only one of them, because the two
    wrong behaviours are otherwise indistinguishable. A refusal that is converted
    to ``False`` withholds that pid and sweeps the other; an exception that
    escapes instead aborts the whole pass, which also leaves the first pid alive
    and would satisfy a single-candidate assertion while skipping every remaining
    pid and everything after the loop.
    """
    from unittest.mock import patch

    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[6161, 6262])

    def raising_for_one(pid: int, *, reason: str, caller: str) -> bool:
        if pid == 6161:
            raise RuntimeError("gate unavailable")
        return True

    with patch("kiro_crew.session_cleanup.authorize_runtime_kill", raising_for_one):
        await service._sweep_periodic_pids()

    assert recorded["pid_kills"] == [6262], (
        "the unanswerable pid is withheld and the answerable one is still swept; "
        f"got {recorded['pid_kills']}"
    )


class _LeaseHolder:
    """One session's claim on a runtime with a given pid."""

    def __init__(self, pid: int) -> None:
        self._runtime = _PidRuntime(pid)
        self._lease: str | None = None

    async def take(self) -> None:
        acquisition = await ro.RUNTIME_OWNERSHIP.acquire(
            ("test", self._runtime.pid),
            f"session:{self._runtime.pid}",
            self._spawn,
        )
        self._lease = acquisition.lease

    @property
    def lease(self) -> str:
        """The lease id, for a test that must hand it to a provider stand-in."""
        assert self._lease is not None, "take() first"
        return self._lease

    async def give_back(self) -> None:
        if self._lease is not None:
            await ro.RUNTIME_OWNERSHIP.release(self._lease)
            self._lease = None

    async def _spawn(self) -> Any:
        return self._runtime


class _PidRuntime:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def is_alive(self) -> bool:
        return True


# ── the scope reaper's gate ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_scope_reaper_does_not_stop_a_unit_holding_a_leased_pid(
    tmp_path: Path,
) -> None:
    """MUTATION TARGET: the ownership pass BEFORE ``stop_unit``.

    Stopping the unit is itself the kill: systemd terminates the whole cgroup. So a
    gate consulted only on the processes that survived the stop is consulted after
    the thing it exists to prevent, and a leased runtime is already dead by then.

    The pre-stop pass QUERIES ownership rather than calling the gate, because the
    gate's allow path writes a kill attribution: a survey that ends in an abort
    would otherwise record a kill of every unleased member that was never
    signalled. So the gate must not be called at all on this path.
    """
    from unittest.mock import patch

    from kiro_crew import session_scope_reap as reap

    ro._reset_for_tests()
    scope = tmp_path / "run-test.scope"
    scope.mkdir()
    (scope / "cgroup.procs").write_text("7171\n7272\n", encoding="utf-8")

    stopped: list[str] = []
    signalled: list[tuple[int, int]] = []
    attributed: list[int] = []

    def signal_owned(
        pid: int, sig: int, members: list[int], scope_dir: Path, proc_root: Path
    ) -> tuple[bool, str]:
        signalled.append((pid, sig))
        return True, ""

    refusals: list[str] = []
    holder = _LeaseHolder(7171)
    await holder.take()
    real_gate = reap.authorize_runtime_kill
    try:
        with patch.object(
            reap,
            "authorize_runtime_kill",
            lambda pid, **kw: (attributed.append(pid) or real_gate(pid, **kw)),
        ):
            cleared = reap._reclaim_scope(
                scope,
                "run-test.scope",
                proc_root=tmp_path,
                stop_unit=lambda unit: stopped.append(unit) or True,
                signal_owned=signal_owned,
                sleep=lambda secs: None,
                on_refusal=refusals.append,
            )
    finally:
        await holder.give_back()

    assert stopped == [], "the unit is never stopped while one of its members is leased"
    assert signalled == [], "and its unleased neighbour is not signalled either"
    assert cleared is False, "the scope was not reclaimed"
    assert refusals == ["still leased"], "the caller can record a refusal, not a failure"
    assert attributed == [], (
        "the survey must not call the attributing gate, or the abort leaves a kill "
        f"recorded for a process nothing signalled; got {attributed}"
    )


def test_the_scope_reaper_signals_every_member_when_none_is_leased(tmp_path: Path) -> None:
    """CONTROL for the test above."""
    from kiro_crew import session_scope_reap as reap

    ro._reset_for_tests()
    scope = tmp_path / "run-plain.scope"
    scope.mkdir()
    (scope / "cgroup.procs").write_text("7171\n7272\n", encoding="utf-8")
    signalled: list[int] = []
    reap._reclaim_scope(
        scope,
        "run-plain.scope",
        proc_root=tmp_path,
        stop_unit=lambda unit: True,
        signal_owned=lambda pid, sig, members, d, p: (signalled.append(pid) or (True, "")),
        sleep=lambda secs: None,
    )
    assert set(signalled) == {7171, 7272}


def test_the_slice_enumerator_reads_every_scope_under_the_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kernel truth is the union of the slice's own members and every scope's."""
    from kiro_crew import session_scope_reap as reap

    slice_dir = tmp_path / "kirocrew-agents-tok.slice"
    slice_dir.mkdir()
    (slice_dir / "cgroup.procs").write_text("11\n", encoding="utf-8")
    for name, body in (("a.scope", "22\n33\n"), ("b.scope", "44\n")):
        scope = slice_dir / name
        scope.mkdir()
        (scope / "cgroup.procs").write_text(body, encoding="utf-8")
    # A sibling that is not a scope must not contribute.
    other = slice_dir / "nested.slice"
    other.mkdir()
    (other / "cgroup.procs").write_text("99\n", encoding="utf-8")

    monkeypatch.setattr(reap, "_instance_scope_dir", lambda: (slice_dir, ""))
    assert reap.instance_slice_pids() == {11, 22, 33, 44}


def test_an_unresolvable_slice_reads_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty is the honest answer, and the reconciler's registry refusal is what
    stops an empty kernel reading from being acted on as an empty install."""
    from kiro_crew import session_scope_reap as reap

    monkeypatch.setattr(reap, "_instance_scope_dir", lambda: (None, "no cgroup dir"))
    assert reap.instance_slice_pids() == set()


# ── the subagent orphan reconcile ────────────────────────────────────────────


@pytest.fixture()
def agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point subagent persistence at a registry below this test's temp directory."""
    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    return root


@pytest.mark.asyncio
async def test_the_orphan_reconcile_withholds_a_leased_pid(agent_root: Path) -> None:
    """MUTATION TARGET: the gate in ``_reconcile_orphans_impl``.

    ``state.json`` is a record this run wrote before the restart and says nothing
    about who is on the process now. On a shared runtime the parent and every
    sibling are on the same pid, so a per-run file naming it is not authority to
    end it. The tombstone is still written -- this run IS over -- which is the
    difference between reporting the run finished and killing the process.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_persistence import create_agent_folder, update_state

    ro._reset_for_tests()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    create_agent_folder("leased-orphan", task="a shared runtime")
    update_state("leased-orphan", pid=8181)

    holder = _LeaseHolder(8181)
    await holder.take()
    try:
        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True),
            patch.object(manager, "_kill_orphan_pid") as mock_kill,
            patch.object(manager, "_notify_orphan", return_value=None),
        ):
            await manager._reconcile_orphans()
    finally:
        await holder.give_back()

    mock_kill.assert_not_called()
    assert (
        agent_root / "leased-orphan" / "tombstone.json"
    ).exists(), "the run is over either way; only the signal is withheld"


# ── the subagent reset ladder ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_reset_ladder_withholds_the_kill_and_the_shared_child_sweep() -> None:
    """MUTATION TARGET: the gate in ``_sigkill_session_impl``.

    The recycle check inside the verified kill answers a different question --
    whether this pid is still the process we recorded -- and a yes to that is not
    a yes to this. With session sharing on, the parent and every sibling live on
    the same root, so killing its tree over one hung reset ends work nobody asked
    to end.

    The escaped-children sweep is withheld WITH the root's signal, not alongside
    it. The recorded child set is the shared root's whole descendant tree, so
    sweeping it after sparing the root would spare the process and kill the
    processes it depends on -- worse than either ending it or leaving it alone.
    The surviving tree is the reconciler's to count, and the refusal is RETURNED
    so the caller's record cannot say the run was reaped.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.process_identity import ProcessHandle
    from kiro_crew.subagent import SubagentManager

    ro._reset_for_tests()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    handle = ProcessHandle(pid=7171, start_id="start-identity", pgid=7171, child_pids={7172: 7172})
    # The lease belongs to a co-tenant, not to this teardown: tearing_down names
    # nothing, so the subject's own release finds nothing to give up and the
    # surviving lease is somebody else's.
    manager._sessions.tearing_down = lambda key: []

    holder = _LeaseHolder(7171)
    await holder.take()
    swept: list[Any] = []
    try:
        with (
            patch("kiro_crew.acp.client._kill_escaped_children", side_effect=swept.append),
            patch("kiro_crew.subagent.kill_verified_process") as mock_verified_kill,
        ):
            failure = await manager._sigkill_session("hung", handle)
    finally:
        await holder.give_back()

    mock_verified_kill.assert_not_called()
    assert swept == [], "the recorded child set is the SHARED tree; sparing the root spares it"
    assert failure and "lease" in failure, (
        "the refusal is the caller's record of a tree left standing; " f"got {failure!r}"
    )


class _TeardownProvider:
    """A provider stand-in the ownership module recognises as a lease holder.

    Recognised by its lease SLOT (`_runtime_lease`, None or a string), which is
    how ``runtime_ownership._lease_holder`` identifies one -- deliberately not by
    having the methods, because a MagicMock answers every hasattr. The release
    delegates to the real table, so this exercises the production
    ``release_session_lease`` seam rather than a reimplementation of it.
    """

    def __init__(self, lease: str) -> None:
        self._runtime_lease: str | None = lease

    async def release_runtime_lease(self) -> None:
        if self._runtime_lease is not None:
            await ro.RUNTIME_OWNERSHIP.release(self._runtime_lease)
            self._runtime_lease = None


@pytest.mark.asyncio
async def test_a_teardown_is_not_refused_by_the_lease_it_is_tearing_down() -> None:
    """MUTATION TARGET: releasing the subject's own lease before asking the gate.

    A reset releases the lease inside ``provider.shutdown()``, so every await
    before it is a point where the teardown can hang and land on this path with
    the lease still held. Asking the gate then lets the session being destroyed
    refuse its own last-resort kill: the wedged process survives, and its pid goes
    on being refused by every other sweep for the gateway's life while the run
    records a false "leased by another tenant".

    The lease here belongs to the very session under teardown, reachable through
    ``tearing_down`` after its pop, so the kill MUST proceed.
    """
    from unittest.mock import patch

    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(pid=9292, start_id="abc", pgid=9292, child_pids={})
    holder = _LeaseHolder(9292)
    await holder.take()
    assert rr._leases_on_pid(9292) == 1, "precondition: the subject holds the only lease"

    provider = _TeardownProvider(holder.lease)
    entry = type(
        "_Entry",
        (),
        {"session": type("_S", (), {"provider": provider})(), "handle": handle},
    )()

    class _Sessions:
        def tearing_down(self, key: str) -> list[object]:
            return [entry]

    class _Svc:
        _sessions = _Sessions()

    killed: list[int] = []

    async def fake_kill(h: Any, *, who: str, key: str, child_helpers: Any) -> None:
        killed.append(h.pid)
        return None

    with (
        patch("kiro_crew.cron.kill_verified_process", fake_kill),
        patch("kiro_crew.session.child_process_helpers", lambda: (None, None, None)),
    ):
        failure = await CronService._sigkill_session(_Svc(), "cron:job", handle, who="Reaper")

    assert killed == [9292], (
        "the teardown's own lease must not withhold its last-resort kill; " f"failure={failure!r}"
    )
    assert failure is None


@pytest.mark.asyncio
async def test_another_tenants_lease_still_withholds_the_cron_kill() -> None:
    """The other side of the same rule: what survives the subject's own release is
    a lease held by a DIFFERENT tenant, and that one still refuses.

    Without this, releasing before the gate could be 'release everything and
    always kill', which is the original bug with extra steps.
    """
    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(pid=9393, start_id="abc", pgid=9393, child_pids={})
    holder = _LeaseHolder(9393)
    await holder.take()

    class _Sessions:
        def tearing_down(self, key: str) -> list[object]:
            return []  # the lease belongs to someone this teardown never popped

    class _Svc:
        _sessions = _Sessions()

    try:
        failure = await CronService._sigkill_session(_Svc(), "cron:job", handle, who="Reaper")
    finally:
        await holder.give_back()

    assert failure == "runtime still leased by another tenant"


@pytest.mark.asyncio
async def test_only_the_lease_of_the_process_being_killed_is_released() -> None:
    """MUTATION TARGET: matching the teardown entry on pid AND on start identity.

    A key can hold several teardowns at once -- a run's own finally reset popped a
    session and hung, then a successor's reset popped its session and hung too --
    which is the case ``tearing_down`` exists for. Releasing every entry's lease
    would hand this kill permission over a sibling's live process.

    Two siblings, each differing from the handle under the knife in exactly ONE
    field, so neither match test can hide behind the other:

    * same start identity, different pid -- only the pid test rejects it, and its
      lease must survive on its own pid;
    * same pid, different start identity -- only the identity test rejects it, and
      because it is a lease on the pid being killed, releasing it wrongly would let
      the kill proceed.
    """
    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    killing = ProcessHandle(pid=9494, start_id="mine", pgid=9494, child_pids={})
    other_pid = ProcessHandle(pid=9595, start_id="mine", pgid=9595, child_pids={})
    other_identity = ProcessHandle(pid=9494, start_id="theirs", pgid=9494, child_pids={})

    by_pid = _LeaseHolder(9595)
    await by_pid.take()
    by_identity = _LeaseHolder(9494)
    await by_identity.take()

    def _entry(handle: Any, lease: str) -> Any:
        return type(
            "_Entry",
            (),
            {
                "session": type("_S", (), {"provider": _TeardownProvider(lease)})(),
                "handle": handle,
            },
        )()

    entries = [_entry(other_pid, by_pid.lease), _entry(other_identity, by_identity.lease)]

    class _Svc:
        _sessions = type("_S", (), {"tearing_down": lambda self, key: entries})()

    try:
        failure = await CronService._sigkill_session(_Svc(), "cron:job", killing, who="Reaper")
        assert failure == "runtime still leased by another tenant", (
            "a lease on this pid held by a DIFFERENT process's teardown still refuses; "
            f"got {failure!r}"
        )
        assert rr._leases_on_pid(9595) == 1, "the sibling's lease was not this teardown's to give"
    finally:
        await by_pid.give_back()
        await by_identity.give_back()


@pytest.mark.asyncio
async def test_the_reset_ladder_kills_when_the_only_lease_is_its_own_subject() -> None:
    """MUTATION TARGET: the ladder's own subject-lease release, matched on handle.

    Same rule as the cron path, and reachable for the same reason: the graceful
    reset that hung is the one holding the lease, because a reset releases it
    inside ``provider.shutdown()``. A ladder that asked the gate first would let
    the sub-agent session refuse the kill of the very runtime it wedged.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.process_identity import ProcessHandle
    from kiro_crew.subagent import SubagentManager

    ro._reset_for_tests()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    handle = ProcessHandle(pid=7373, start_id="start-identity", pgid=7373, child_pids={})
    holder = _LeaseHolder(7373)
    await holder.take()
    entry = type(
        "_Entry",
        (),
        {
            "session": type("_S", (), {"provider": _TeardownProvider(holder.lease)})(),
            "handle": handle,
        },
    )()
    manager._sessions.tearing_down = lambda key: [entry]

    with patch("kiro_crew.subagent.kill_verified_process", return_value=None) as mock_verified_kill:
        failure = await manager._sigkill_session("hung", handle)

    mock_verified_kill.assert_called_once()
    assert failure is None, f"the subject's own lease must not withhold the kill; got {failure!r}"


@pytest.mark.asyncio
async def test_the_sweep_gate_audits_both_outcomes() -> None:
    """MUTATION TARGET: the SEL audit on the cleanup sweep's gate decisions.

    The ownership gate writes a log line and nothing else, so an allow that is not
    audited leaves a signalled process with no record of who decided it, and a
    refusal that is not audited leaves an operator reading a non-zero leak count
    with nothing saying why nothing was done.
    """
    from unittest.mock import patch

    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[6161, 6262])
    events: list[tuple[int, str]] = []

    holder = _LeaseHolder(6161)
    await holder.take()
    try:
        with patch(
            "kiro_crew.session_cleanup.audit_kill_decision",
            lambda pid, outcome, reason, *, tool_name: events.append((pid, outcome)),
        ):
            await service._sweep_periodic_pids()
    finally:
        await holder.give_back()

    assert recorded["pid_kills"] == [6262], "the leased pid is withheld"
    assert (6161, "refused") in events, f"the refusal is audited; got {events}"
    assert (6262, "killed") in events, f"the allow is audited too; got {events}"


def test_the_shared_audit_emitter_reaches_sel_and_cannot_break_a_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSITIVE CONTROL for the shared emitter, plus its fail-open rule."""
    logged: list[dict[str, Any]] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs: Any) -> None:
            logged.append(kwargs)

    from kiro_crew import process_identity

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    process_identity.audit_kill_decision(4242, "refused", "because", tool_name="a-caller")
    assert logged and logged[0]["tool_name"] == "a-caller"
    assert logged[0]["tool_kind"] == "process_kill"
    assert logged[0]["outcome"] == "refused"
    assert "4242" in logged[0]["resources"]

    def boom() -> Any:
        raise RuntimeError("sel is down")

    monkeypatch.setattr("kiro_crew.sel.sel", boom)
    process_identity.audit_kill_decision(4242, "killed", "because", tool_name="a-caller")


# ── the cron reaper ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_cron_reaper_reports_a_leased_runtime_instead_of_killing_it() -> None:
    """MUTATION TARGET: the gate in ``CronService._sigkill_session``.

    A cron runtime is not a chat-pool tenant, but the sub-agents a cron turn
    dispatched run on it and take their own leases -- and this path fires when the
    graceful reset hung, which is exactly when one of them is still working.

    The refusal is RETURNED, not swallowed, so the run is never recorded as reaped
    over a process tree that is still standing.
    """
    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(pid=9191, start_id="abc", pgid=9191, child_pids={})

    class _Svc:
        _sessions = type("_S", (), {"tearing_down": lambda self, key: []})()

    holder = _LeaseHolder(9191)
    await holder.take()
    try:
        failure = await CronService._sigkill_session(_Svc(), "cron:job", handle, who="Reaper")
    finally:
        await holder.give_back()
    assert failure is not None and "leased" in failure
