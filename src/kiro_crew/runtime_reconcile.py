"""Reconcile the kernel's process list against the runtime ownership registry.

Every other sweep in this gateway asks one question -- "is this pid one I still
need?" -- and asks it of a record. This one asks the kernel instead, and compares
the two answers in BOTH directions, because each direction is a different leak:

alive but unowned
    A process runs inside this install's own agent slice and no record claims it.
    Nothing is ever going to end it: the sweeps that could are driven by the very
    records it is missing from, so it survives every pass and every gateway
    restart. Measured on a live instance: 20 such processes at rest, 57-97 under
    load.

owned but dead
    A record names a pid that is gone, or one the kernel has given to an unrelated
    process. Whoever holds the lease is still waiting on a runtime that cannot
    answer, and the record keeps a pid number reserved against a process that will
    never be signalled. Measured on a live instance: 0 to 23 over fifteen minutes
    of ordinary use.

    The recycled pid belongs in this direction and not in the healthy one, which
    is what makes the reading a usable safety number: a record whose pid now
    names a stranger is the exact input that turns a sweep signalling by pid into
    a kill of somebody else's process. It is counted, its record is retracted,
    and the stranger itself is never signalled -- the record proves only that our
    process is gone, never that the current holder is ours.

The two counts are published as ``unowned_alive`` and ``owned_dead`` so an
operator sees a leak while it is small. They are the SLI; the actions below are
what makes a non-zero reading temporary rather than permanent.

Why an unowned process is not simply killed
------------------------------------------
Absence from the record is evidence that something is unclaimed, NOT evidence
that it is abandoned. The unowned population on a healthy host is dominated by
processes with perfectly good owners that no *session* record describes: a
Playwright chromium tree owned by a browser panel, ``mcp start-server``
processes owned by a stub connection, sandbox shim wrappers owned by the spawn
in progress. Killing on first sight would take a user's live browser out from
under them and call it a leak fixed.

So a kill needs four independent things to line up, and any one of them missing
leaves the process alone and merely counted:

1. the process carries this install's own spawn marker, so it is ours to end;
2. it was unowned on the PREVIOUS pass too, which is what distinguishes an
   abandoned process from one whose record is a few milliseconds behind its
   spawn -- the window every registration has;
3. it is older than :data:`DEFAULT_MIN_AGE_SECS`, for the same window seen from
   the other side;
4. :func:`~kiro_crew.runtime_ownership.authorize_runtime_kill` allows it, so a
   pid that turns out to be leased after all is refused at the last moment and
   the refusal is logged.

A pass also spends at most :data:`DEFAULT_MAX_KILLS` kills, so a reconciler that
is wrong about a whole population is wrong slowly enough to be noticed.

Why the dead direction acts immediately
---------------------------------------
Forgetting a dead pid harms nothing -- the process is already gone -- and the
lease holder learns its runtime died instead of waiting out a timeout. The same
two-pass confirmation is therefore not needed here, and a single liveness probe
that says DEAD is not enough on its own: an unsignalable pid is an unknown, not
an absence, and is left alone.

The three population names are the ones ``test/e2e/process_inventory.py`` uses
for the same reconciliation read from outside the process, and they classify the
recycled pid the same way, so a reading published here and an inventory taken by
that harness describe one host state in one vocabulary.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import platform_compat, session_pid
from kiro_crew.mcp_gateway.daemon_control import configured_socket_path
from kiro_crew.process_identity import audit_kill_decision
from kiro_crew.runtime_ownership import RUNTIME_OWNERSHIP, authorize_runtime_kill
from kiro_crew.session_pid import (
    _pid_age_seconds,
    _pid_start_token,
    _read_env_has_kirocrew_marker,
)
from kiro_crew.session_scope_reap import instance_slice_pids

logger = logging.getLogger(__name__)

#: How old an unowned process must be before it can be killed. A spawn publishes
#: its record after the process exists, so anything younger may simply be a
#: registration in flight.
DEFAULT_MIN_AGE_SECS = 300.0

#: Kills one pass may perform. A reconciler that has misjudged an entire
#: population reaches this ceiling and stops, leaving the rest counted and the
#: operator a reading to act on.
DEFAULT_MAX_KILLS = 5


@dataclass(frozen=True, slots=True)
class ReconcileReading:
    """What one pass saw and did. ``supported`` false means it could not look."""

    supported: bool = True
    reason: str = ""
    #: Pids a record claims and the kernel still has, as the same process.
    owned_alive: int = 0
    #: Pids a record claims that are gone, or that now name a stranger.
    owned_dead: int = 0
    #: Live pids inside our own agent slice that no record claims.
    unowned_alive: int = 0
    #: Unowned pids that met all four conditions and were signalled.
    killed: int = 0
    #: Dead pids whose records were retracted.
    forgotten: int = 0
    #: Unowned pids held back, with the reason each was held.
    withheld: tuple[tuple[int, str], ...] = field(default_factory=tuple)

    def as_counter_fields(self) -> dict[str, str | int | bool | float]:
        """The reading as metric fields, named to match the liveness SLI."""
        return {
            "unowned_alive": self.unowned_alive,
            "owned_dead": self.owned_dead,
            "owned_alive": self.owned_alive,
            "killed": self.killed,
            "forgotten": self.forgotten,
        }


def _sel_reconcile_kill(pid: int, outcome: str, reason: str) -> None:
    """This module's kill-decision audit, on the shared emitter.

    Shared with the cleanup sweeps so one change to the audit shape reaches every
    kill decision in this gateway rather than one of them.
    """
    audit_kill_decision(pid, outcome, reason, tool_name="runtime_reconcile")


def _session_pid_entry_owners() -> dict[int, tuple[int, str | None]]:
    """``{child_pid: (owning gateway pid, recorded start token)}`` for EVERY row.

    The whole session-tracking file, not one gateway's slice of it. A reconciler
    that compares records against the kernel sees pids this gateway never filed --
    a concurrent ``kirocrew chat`` on the same data home, a predecessor gateway
    whose rows outlived it -- and needs two things about each: its recorded
    identity, so a recycled pid is detectable, and its OWNER, because
    ``session_pid._untrack_session_pid`` matches on the CALLING process's own
    prefix and cannot remove anybody else's row. Without the owner this module
    would call the untracker for a foreign row, get ``True`` back from a rewrite
    that changed nothing, and report a retraction that never happened.

    Read under the file's own lock, through that module's path and lock helpers, so
    it observes the same exclusion every other reader does. A later row for the same
    child pid wins, which is the precedence a line-ordered rewrite gives. Tolerant
    of malformed lines for the reason every reader of this file is: a live gateway
    appends to it concurrently.
    """
    owners: dict[int, tuple[int, str | None]] = {}
    path = session_pid._session_pid_file_path()
    try:
        with session_pid._session_pid_file_lock():
            if not path.exists():
                return owners
            lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.warning("runtime_reconcile: could not read %s", path, exc_info=True)
        return owners
    for line in lines:
        parts = line.strip().split(":")
        if len(parts) not in (2, 3):
            continue
        try:
            gw_pid, child_pid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if child_pid <= 0:
            continue
        owners[child_pid] = (gw_pid, parts[2] or None if len(parts) == 3 else None)
    return owners


def _pid_identity(pid: int) -> str | None:
    """*pid*'s process-start identity, or ``None`` when it cannot be read.

    The same token every other reader in this repository compares a pid against
    (:func:`session_pid._pid_start_token`), so a mismatch here means what it means
    everywhere else: the kernel gave this number to a different process.
    """
    return _pid_start_token(pid)


def _leases_on_pid(pid: int) -> int:
    """Leases live runtimes on *pid* hold, asked through the lease table itself.

    A pid is all this reconciler has: it starts from the kernel's list of process
    ids, not from runtime objects, so the identity-first accessor has nothing
    stronger to prefer here. Asked through ``RUNTIME_OWNERSHIP`` because that is
    the table's own published surface, and a reconciler that reaches for a
    module-level convenience instead would break on a rename of a name it does
    not own.

    A dead runtime's leases do not count, which is what makes this a gate and not
    a lock: a process that has already exited cannot be harmed by a signal, while
    refusing to signal it would suppress the reap of its zombie and the sweep of
    the descendants that escaped its group.
    """
    return RUNTIME_OWNERSHIP.leases_on_pid(pid)


def process_is_ours(pid: int, *, proc_root: Path | None = None) -> bool:
    """Whether *pid* carries this install's spawn marker.

    The marker is an environment variable set on every process Kiro Crew spawns
    and inherited by its whole tree, so it answers for a descendant the spawn
    never recorded -- which is most of what an unowned reading is made of. It is
    read out of the kernel's exec-time copy rather than from any file this uid
    can write, because it is the one thing standing between a reconciler and a
    stranger's process: a same-uid process can forge a file or an argv, and
    cannot alter another process's exec-time environment.

    Fail-closed through :func:`session_pid._read_env_has_kirocrew_marker`'s
    tri-state answer: an unreadable environment is not ours, and a platform with
    no environ oracle has nothing that is, which makes the kill direction a
    no-op there rather than a guess.
    """
    return _read_env_has_kirocrew_marker(pid, proc_root) is True


def process_age_secs(pid: int, *, proc_root: Path = Path("/proc")) -> float:
    """Seconds since *pid*'s process started, or ``0.0`` when unreadable.

    Delegates to :func:`session_pid._pid_age_seconds`, which reads the start time
    out of ``/proc/<pid>/stat`` field 22 on Linux and the process-start id on
    macOS. The directory's own ``st_ctime`` is NOT the process's age: procfs
    assigns it when the inode is instantiated, which can be long after the process
    started, so an age taken from it reads every candidate as younger than the
    floor and the kill arm never reclaims anything.

    Zero on failure is the fail-closed answer: it reads as "too young to touch"
    against :data:`DEFAULT_MIN_AGE_SECS`, so a pid whose age cannot be established
    is never old enough to kill.
    """
    age = _pid_age_seconds(pid, str(proc_root))
    if age is None:
        return 0.0
    return max(0.0, age)


class RuntimeReconciler:
    """One reconciliation pass, and the memory of the previous one.

    Every seam is injected so the whole decision path runs against a fake kernel
    with no real processes and no real signals. The instance is retained across
    passes because the two-pass confirmation IS its state: a pid unowned once is
    remembered, and only an unowned pid remembered from last time can be killed.
    """

    def __init__(
        self,
        *,
        slice_pids: Callable[[], set[int]],
        recorded_pids: Callable[[], set[int]],
        is_alive: Callable[[int], bool],
        identity_of: Callable[[int], str | None] = _pid_identity,
        was_recycled: Callable[[int], bool] = lambda _pid: False,
        is_ours: Callable[[int], bool] = process_is_ours,
        leases_on: Callable[[int], int] = _leases_on_pid,
        authorize: Callable[[int, str], bool] | None = None,
        kill_tree: Callable[[int], int],
        forget: Callable[[int], bool],
        notify_dead: Callable[[int], None],
        audit: Callable[[int, str, str], None] = _sel_reconcile_kill,
        age_secs: Callable[[int], float] = process_age_secs,
        min_age_secs: float = DEFAULT_MIN_AGE_SECS,
        max_kills: int = DEFAULT_MAX_KILLS,
    ) -> None:
        self._slice_pids = slice_pids
        self._recorded_pids = recorded_pids
        self._is_alive = is_alive
        self._identity_of = identity_of
        self._was_recycled = was_recycled
        self._is_ours = is_ours
        self._leases_on = leases_on
        self._authorize = authorize or _default_authorize
        self._kill_tree = kill_tree
        self._forget = forget
        self._notify_dead = notify_dead
        self._audit = audit
        self._age_secs = age_secs
        self._min_age_secs = min_age_secs
        self._max_kills = max_kills
        #: Unowned pids seen on the previous pass, each with the process identity
        #: it had then. The second half of the two-pass confirmation, keyed on
        #: identity as well as number so a recycled pid cannot inherit it.
        self._unowned_last_pass: dict[int, str | None] = {}
        #: Pids whose holder has already been told their runtime died. A
        #: notification is a user-visible event and is owed once, while the
        #: owned-dead COUNT stays a fresh reading on every pass.
        self._notified_dead: set[int] = set()
        #: Process identity per candidate, captured when this pass classified it
        #: and re-compared in the instant before the signal.
        self._identities: dict[int, str | None] = {}
        #: The reason each withheld pid carried on the previous pass, so the audit
        #: records a CHANGE of reason rather than a steady state repeated forever.
        self._withheld_reason: dict[int, str] = {}

    def run_once(self) -> ReconcileReading:
        """Compare both truths, act on what only one of them knows, and report."""
        try:
            kernel = self._slice_pids()
        except Exception as exc:
            return ReconcileReading(supported=False, reason=f"cannot read the agent slice: {exc}")
        try:
            recorded = self._recorded_pids()
        except Exception as exc:
            # A registry that cannot be read makes EVERY live pid look unowned,
            # which is the one input that turns this pass into a massacre. Refuse
            # the whole pass rather than acting on half of it.
            return ReconcileReading(supported=False, reason=f"cannot read the registry: {exc}")

        owned_dead, forgotten = self._reconcile_dead(recorded)
        unowned = self._unowned(kernel, recorded)
        # Identity captured at classification, for the re-check in the instant
        # before each signal. Rebuilt per pass so a pid that left the population
        # leaves no stale identity behind.
        self._identities = {}
        for pid in unowned:
            try:
                self._identities[pid] = self._identity_of(pid)
            except Exception:
                logger.debug("runtime_reconcile: identity read failed pid=%s", pid, exc_info=True)
                self._identities[pid] = None
        killed, withheld = self._reconcile_unowned(unowned)
        # The two-pass memory is keyed on pid AND identity. Keyed on the number
        # alone, a pid that exited and was reused between passes would inherit the
        # previous pass's confirmation and be eligible immediately -- a
        # confirmation about a process that has since gone.
        self._unowned_last_pass = dict(self._identities)
        return ReconcileReading(
            owned_alive=len(recorded) - owned_dead,
            owned_dead=owned_dead,
            unowned_alive=len(unowned),
            killed=killed,
            forgotten=forgotten,
            withheld=tuple(withheld),
        )

    # -- direction one: a record with no process --

    def _reconcile_dead(self, recorded: set[int]) -> tuple[int, int]:
        dead = 0
        forgotten = 0
        for pid in sorted(recorded):
            try:
                if self._is_alive(pid) and not self._is_stranger(pid):
                    continue
            except Exception:
                # An unreadable liveness probe is an unknown process, not an
                # absent one. Retracting a record on that answer is how a live
                # runtime loses the only thing that can ever find it again.
                continue
            dead += 1
            try:
                if self._forget(pid):
                    forgotten += 1
                else:
                    # The untracker reports whether its rewrite landed. A refused
                    # rewrite leaves the stale line in place, so counting it as a
                    # retraction would report work that did not happen. The stale
                    # entry is re-detected and re-pruned on the next pass.
                    logger.warning(
                        "runtime_reconcile: the record for pid=%s could not be retracted", pid
                    )
            except Exception:
                logger.debug("runtime_reconcile: could not retract pid=%s", pid, exc_info=True)
            if pid in self._notified_dead:
                # The count above is a true reading and stays honest every pass,
                # but the notification is a user-visible event and fires ONCE per
                # pid. Retraction does not always remove the pid from every record
                # -- one held in the manager's own live union is not in the file
                # the untracker rewrites -- so the same disagreement can be
                # rediscovered on every pass, and a notice per pass per cleanup
                # interval would keep appending to the user's chat for as long as
                # the gateway runs.
                continue
            self._notified_dead.add(pid)
            try:
                self._notify_dead(pid)
            except Exception:
                logger.debug("runtime_reconcile: could not notify for pid=%s", pid, exc_info=True)
        if dead:
            logger.warning(
                "runtime_reconcile owned_dead=%d forgotten=%d: records named processes "
                "that are gone or whose pid now belongs to a stranger",
                dead,
                forgotten,
            )
        return dead, forgotten

    def _is_stranger(self, pid: int) -> bool:
        """Whether the live process at *pid* is not the one the record named.

        A recorded start identity that DIFFERS from the live one proves the kernel
        reallocated the pid, so the tracked process is gone even though something
        answers at that number. An identity that cannot be read on either side is
        an unknown and never a mismatch, which is the rule
        ``session_pid._pid_start_token`` states for every reader of that token: a
        pid wrongly called a stranger would have its record retracted while its
        runtime is still serving.

        Subtractive only, exactly as that token is allowed to be used. A stranger
        verdict retracts a record; it never authorizes a signal, because the
        current holder of the pid is by definition not ours.
        """
        try:
            return self._was_recycled(pid)
        except Exception:
            logger.debug("runtime_reconcile: recycle check failed pid=%s", pid, exc_info=True)
            return False

    # -- direction two: a process with no record --

    def _unowned(self, kernel: set[int], recorded: set[int]) -> list[int]:
        mine = os.getpid()
        out: list[int] = []
        for pid in sorted(kernel):
            if pid <= 1 or pid == mine or pid in recorded:
                continue
            try:
                if self._leases_on(pid):
                    continue
            except Exception:
                # The lease table is the authority on whether a pid is claimed.
                # Unreadable means claimed.
                continue
            out.append(pid)
        return out

    def _reconcile_unowned(self, unowned: Iterable[int]) -> tuple[int, list[tuple[int, str]]]:
        killed = 0
        withheld: list[tuple[int, str]] = []
        reasons_now: dict[int, str] = {}

        def hold(pid: int, why: str) -> None:
            """Record a withheld pid, auditing only a CHANGE of reason.

            Most of the unowned population is permanently withheld -- every MCP
            server and sandbox helper in the slice sits at "kill signalled
            nothing" forever -- so one event per pid per pass would write
            thousands of identical rows a day into a log with a finite rotation
            ceiling, evicting the tool-invocation history an operator actually
            needs. A transition is the event; a steady state is not.
            """
            withheld.append((pid, why))
            reasons_now[pid] = why
            if self._withheld_reason.get(pid) != why:
                self._audit(pid, "refused", why)

        for pid in unowned:
            if killed >= self._max_kills:
                hold(pid, "kill budget spent")
                continue
            reason = self._why_not_yet(pid)
            if reason:
                hold(pid, reason)
                continue
            # The identity read at classification, re-read HERE, immediately
            # before the signal. Everything above -- two passes, the marker, the
            # age floor -- inspected a pid NUMBER, and the kernel may hand that
            # number to another process at any point after each check.
            # Re-comparing the process-start identity narrows the exposure to
            # this comparison and the signal that follows it; the kill seam then
            # re-applies its own managed-agent check inside that gap, so a
            # replacement that is not one of ours is never signalled at all.
            if self._identity_changed(pid):
                hold(pid, "process identity changed since classification")
                continue
            # The gate LAST, because its allow path writes the kill attribution:
            # asked any earlier, every pid the checks above still withhold would
            # be recorded as a kill that never happened.
            if not self._authorize(pid, "unowned process inside our agent slice"):
                hold(pid, "refused by the ownership gate")
                continue
            try:
                signalled = self._kill_tree(pid)
            except Exception:
                withheld.append((pid, "kill failed"))
                reasons_now[pid] = "kill failed"
                self._audit(pid, "failed", "the kill raised")
                logger.debug("runtime_reconcile: kill failed pid=%s", pid, exc_info=True)
                continue
            if not signalled:
                # The tree-kill seam re-applies its own recycle guard and signals
                # NOTHING when the pid does not look like a managed agent process.
                # That is most of what an unowned reading is made of, so counting
                # an unsignalled call as a kill would spend the whole per-pass
                # budget on the same lowest pids every pass, forever, while the
                # SLI reported five kills and nothing changed.
                hold(pid, "kill signalled nothing")
                continue
            killed += 1
            self._audit(pid, "killed", "unowned on two passes, our marker, past the age floor")
            logger.warning(
                "runtime_reconcile killed pid=%s: unowned on two consecutive passes, "
                "carries our spawn marker, older than %.0fs",
                pid,
                self._min_age_secs,
            )
        if withheld:
            logger.info(
                "runtime_reconcile: %d unowned process(es) counted and left alone (%s)",
                len(withheld),
                ", ".join(sorted({why for _pid, why in withheld})),
            )
        # Only this pass's reasons are retained, so a pid that leaves the
        # population and comes back is a transition again rather than inheriting a
        # reason nothing recorded.
        self._withheld_reason = reasons_now
        return killed, withheld

    def _identity_changed(self, pid: int) -> bool:
        """Whether *pid* names a different process now than at classification.

        The identity is captured for every candidate when the pass classifies it
        and compared again in the instant before the signal. An identity that
        cannot be read on either side counts as CHANGED: an unreadable process is
        one this pass cannot claim to have inspected, and the fail-closed answer
        costs a withheld kill the next pass can retry.
        """
        recorded = self._identities.get(pid)
        if recorded is None:
            return True
        try:
            return self._identity_of(pid) != recorded
        except Exception:
            logger.debug("runtime_reconcile: identity re-read failed pid=%s", pid, exc_info=True)
            return True

    def _why_not_yet(self, pid: int) -> str:
        """Empty when every precondition for killing *pid* holds."""
        if pid not in self._unowned_last_pass:
            return "first pass unowned"
        if self._unowned_last_pass[pid] != self._identities.get(pid):
            # Same number, different process: the confirmation the previous pass
            # earned belongs to a process that has since exited.
            return "first pass unowned"
        try:
            if not self._is_ours(pid):
                return "no spawn marker"
        except Exception:
            return "no spawn marker"
        try:
            if self._age_secs(pid) < self._min_age_secs:
                return "younger than the age floor"
        except Exception:
            return "younger than the age floor"
        return ""


def _default_authorize(pid: int, reason: str) -> bool:
    return authorize_runtime_kill(pid, reason=reason, caller="runtime_reconcile")


def _mcp_backend_pids() -> set[int]:
    """Pids of the MCP backends gatewayd is hosting, from its own pidfile.

    A second record, and not an optional one: an MCP backend lives in gatewayd's
    pool, not in any session map, so every backend would read as unowned against
    the session registry alone -- and backends are a large, long-lived population
    that carries the same spawn marker a reconciler kill requires. gatewayd
    publishes them beside its socket precisely so a process outside it can answer
    for them, which is what makes this readable from the gateway.

    RAISES rather than answering an empty set when a file that EXISTS cannot be
    read, so a corrupt or unreadable pidfile refuses the pass
    (:meth:`RuntimeReconciler.run_once` turns a registry error into
    ``supported=False``) instead of presenting every live backend as an unowned
    process.

    An ABSENT file is a different answer and a real one: nothing is hosted. No
    broker runs at all under the default empty stub configuration, gatewayd
    unlinks the file on a clean shutdown, and it appears only a heartbeat after
    start -- so treating absence as a refusal would leave both directions and the
    liveness SLI permanently inert behind one debug line, which is the quietest
    possible way for this module to do nothing. A file that reads as empty says
    the same thing: gatewayd is up and hosts nothing.
    """
    path = Path(f"{configured_socket_path()}.backends")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    pids: set[int] = set()
    for token in raw.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 1:
            pids.add(pid)
    return pids


def build_reconciler(
    *,
    active_pids: Callable[[], set[int]],
    notify_dead: Callable[[int], None],
    min_age_secs: float = DEFAULT_MIN_AGE_SECS,
    max_kills: int = DEFAULT_MAX_KILLS,
) -> RuntimeReconciler:
    """The reconciler wired to this gateway's real records and real signals.

    *active_pids* is the SessionManager's own live-pid union -- only the manager
    knows it -- and is passed as a callable so each pass reads it fresh rather
    than reconciling against a set gathered an interval ago.

    The kill seam is :func:`session_pid._kill_pid_tree`, which re-applies its own
    argv recycle guard immediately before signalling. Reusing it rather than
    reaching for a kill primitive is deliberate twice over: the recycle guard is
    a different question from ownership and both must be answered, and it adds no
    new unattributed primitive call site to the repo.
    """

    def entry_index() -> dict[int, tuple[int, str | None]]:
        """``{pid: (owning gateway pid, recorded start identity)}`` for every row.

        Read across EVERY gateway's rows, not just this process's, for the same
        reason membership is: a row filed by a concurrent CLI or a predecessor
        gateway on this data home names a pid in the very slice this reconciler
        reads. Its identity is what the recycle check needs, and its OWNER is what
        decides whether a retraction here can actually remove it.
        """
        return _session_pid_entry_owners()

    #: The session file as this pass read it. Refreshed by ``recorded()``, which
    #: ``run_once`` calls once at the top of every pass, so the identities the
    #: recycle check compares against are the ones the pass classified -- and one
    #: locked file read serves the whole pass instead of one per recorded pid.
    snapshot: dict[int, tuple[int, str | None]] = {}

    def recorded() -> set[int]:
        """Every pid any record on this data home claims, and its identities.

        The membership question and the kernel question must be asked at the SAME
        scope or the difference between them is not a leak. The kernel side is
        scoped to the DATA HOME -- the agent slice is named from a hash of the
        config directory -- so a second process on the same home puts its agent
        runtimes in the very slice this reconciler reads: a ``kirocrew chat`` or
        ``run`` doing agent work in-process (the gateway lock bars a second
        gateway, not a second CLI), and this gateway's own namespace-sandbox
        children, which appear only in the descendant pid file because the pid
        that is tracked and leased is the launcher parent.

        Membership therefore comes from BOTH pid files across EVERY gateway pid.
        Asking only for this process's own session entries would leave every one of
        those processes unclaimed by construction: their records are filed under a
        different gateway pid or in the other file, and the lease table is
        per-process memory that cannot see them either. They would pass the marker
        test (it is inherited), age past the floor, and be signalled -- the exact
        harm this module exists to prevent, delivered by it.

        The session-file snapshot carries each row's OWNER and start identity: the
        identity is what the recycle check compares against, and the owner is what
        decides whether a retraction here can remove the row at all. Membership does
        not come from it.
        """
        snapshot.clear()
        snapshot.update(entry_index())
        tracked, complete = session_pid._read_tracked_agent_pids()
        if not complete:
            # A partial read of the tracking files is the one input that makes a
            # live runtime look unowned, so it refuses the pass rather than
            # authorizing a kill on incomplete membership. The same completeness
            # requirement the scope reaper imposes on kill-authorizing callers.
            raise RuntimeError("the tracked-pid snapshot is incomplete")
        return set(active_pids()) | _mcp_backend_pids() | tracked | set(snapshot)

    def was_recycled(pid: int) -> bool:
        recorded_token = snapshot.get(pid, (0, None))[1]
        if not recorded_token:
            # No entry, or a legacy two-field entry with no identity recorded.
            # Nothing to compare, so nothing is proven.
            return False
        live_token = session_pid._pid_start_token(pid)
        if not live_token:
            # Unknown on the live side. Never a mismatch.
            return False
        return live_token != recorded_token

    def is_alive(pid: int) -> bool:
        # PID_UNSIGNALABLE is an unknown, not a death. Only a confirmed
        # PID_DEAD retracts a record, so a permission-denied probe leaves the
        # record standing for the next pass.
        return platform_compat.pid_liveness(pid) != platform_compat.PID_DEAD

    def kill_tree(pid: int) -> int:
        # The count is READ, not discarded: this seam re-applies its own recycle
        # guard and signals nothing for a pid that does not look like a managed
        # agent process, and a caller that ignored that would count no-ops as
        # kills.
        total, _root = session_pid._kill_pid_tree(pid)
        return total

    def forget(pid: int) -> bool:
        """Retract this pid's record, and report whether a row was actually removed.

        ``_untrack_session_pid`` matches on ``<this gateway pid>:<pid>``, so it can
        only remove a row THIS gateway filed. A row owned by a concurrent CLI or a
        predecessor gateway is a real dead record and is counted as one, but nothing
        here can remove it -- and an unchanged rewrite still reports success, so
        calling the untracker for a foreign row would report a retraction that did
        not happen. Those rows are the next gateway start's to clear, once its owner
        reads dead.
        """
        owner = snapshot.get(pid, (0, None))[0]
        if owner and owner != os.getpid():
            return False
        return session_pid._untrack_session_pid(pid)

    return RuntimeReconciler(
        slice_pids=instance_slice_pids,
        recorded_pids=recorded,
        is_alive=is_alive,
        was_recycled=was_recycled,
        kill_tree=kill_tree,
        forget=forget,
        notify_dead=notify_dead,
        min_age_secs=min_age_secs,
        max_kills=max_kills,
    )
