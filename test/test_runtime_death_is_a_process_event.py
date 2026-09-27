"""A runtime death is a process event: classified once, read by every tenant.

Field motivation, and every number here is measured rather than supposed. One
gateway lost a shared runtime carrying 15 sessions, 8 of them mid-prompt, and each
of the eight recorded the loss as its OWN failure -- charging its own retry
budget, marching its own circuit breaker, advancing its own auto-pause counter. A
second such death three hours later disturbed 10 more. Across that window every
sampled death carried ``rc=-15``, an ordinary SIGTERM teardown, while the ``HTTP
404`` registration line pasted onto the cards as the death REASON appears in 3 of
41 deaths and once on a runtime that does not die at all.

So two things are pinned here:

* the death is announced ONCE, where it is detected, with what the process was
  carrying at that instant -- and each consumer reads that one record instead of
  forming its own account (:mod:`kiro_crew.runtime_death`);
* the reason names the exit status, and a stderr line becomes the stated CAUSE
  only when it matches a signature that describes a death.

Single-tenant runtimes -- every runtime at ``CHAT_RUNTIME_CAP`` 1 with no
sub-agent on it -- are asserted UNCHANGED throughout, because that is the
behaviour-preservation bar this refactor carries.
"""

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import runtime_death, runtime_ownership
from kiro_crew.acp.runtime import AcpRuntime, _proven_death_cause, _rc_phrase

# The exact stderr those deaths carry, and the line wrongly promoted to their
# cause. Kept verbatim so a future edit to the signature table
# is tested against the real text rather than a paraphrase of it.
COE_STDERR = [
    "Warning: Failed to update agent config auto-improvement-engineer.json: "
    "/home/x/.kiro/agents: Read-only file system",
    "Dynamic registration failed: Registration failed: HTTP 404 Not Found",
]


@pytest.fixture(autouse=True)
def _clean_death_state():
    runtime_death._reset_for_tests()
    yield
    runtime_death._reset_for_tests()


def _runtime(
    *, sessions: int = 1, stderr: list[str] | None = None, rc: int | None = -15
) -> AcpRuntime:
    """A runtime with *sessions* ACP sessions multiplexed on it and a set exit code.

    ``rc=None`` leaves the child UNREAPED, which is what ``is_alive()`` reads as
    still running -- the state a runtime is really in when the reader loop sees
    EOF and marks the death.
    """
    rt = AcpRuntime(memory_mode="persistent")
    rt._process = SimpleNamespace(returncode=rc, stdin=None)
    for n in range(sessions):
        rt._session_queues[f"s{n}"] = asyncio.Queue()
    if stderr:
        rt._stderr_lines.extend(stderr)
    return rt


# ───────────────────────── the death reason (item d) ─────────────────────────


def test_signal_death_names_its_signal():
    """``rc=-15`` alone is a number to look up; the name is what makes an
    ordinary teardown legible as one instead of reading like a crash."""
    assert _rc_phrase(-15) == "rc=-15 (signal SIGTERM)"
    assert _rc_phrase(-9) == "rc=-9 (signal SIGKILL)"


def test_ordinary_exit_codes_are_unchanged():
    """Only a negative returncode is a signal, so nothing else grows a name --
    including the ``?`` a never-spawned process yields and a signal number the
    platform does not know."""
    assert _rc_phrase(1) == "rc=1"
    assert _rc_phrase(0) == "rc=0"
    assert _rc_phrase("?") == "rc=?"
    assert _rc_phrase(-999) == "rc=-999"


def test_the_coe_404_line_is_not_a_death_cause():
    """THE regression this closes. Neither line of that stderr describes a
    death, so the reason stays the exit status."""
    assert _proven_death_cause("\n".join(COE_STDERR)) is None
    rt = _runtime(stderr=COE_STDERR)
    reason = rt._exit_reason(-15)
    assert reason == "process exited (rc=-15 (signal SIGTERM))"
    assert "404" not in reason


def test_a_throttle_signature_still_earns_the_cause_slot():
    """An explicit signature is promoted even when it is NOT the last line --
    which line a child flushed last is a race with its own buffering and says
    nothing about which line matters."""
    stderr = [
        "Dynamic registration failed: Registration failed: HTTP 429 Too Many Requests",
        "shutting down",
    ]
    rt = _runtime(stderr=stderr)
    reason = rt._exit_reason(-15)
    assert "HTTP 429" in reason
    assert reason.startswith("process exited (rc=-15 (signal SIGTERM)): ")


def test_an_enospc_signature_still_earns_its_operator_hint():
    """The case the appended tail was added for in the first place: a bare
    ``rc=1`` told nobody that the runtime tmpfs was full."""
    rt = _runtime(rc=1, stderr=["mkdir failed: No space left on device", "goodbye"])
    reason = rt._exit_reason(1)
    assert "No space left on device" in reason
    assert "kirocrew doctor" in reason


def test_the_unexplained_tail_is_demoted_to_debug_not_dropped(caplog):
    """Demoted, not lost: a reader already looking at this runtime still gets
    the line, and the composed summary keeps its own labelled stderr_tail."""
    rt = _runtime(stderr=COE_STDERR)
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._exit_reason(-15)
    assert "HTTP 404" in caplog.text
    assert "exit stderr tail" in caplog.text


def test_a_restricted_session_still_retains_nothing():
    """Retention outranks diagnostics: a session that retains nothing must not
    gain a cause, a tail or a debug line."""
    rt = _runtime(rc=1, stderr=["mkdir failed: No space left on device"])
    rt.recording_allowed = False
    assert rt._exit_reason(1) == "process exited (rc=1)"


# ─────────────────── one death, one record, N readers (item a) ───────────────────


def test_a_solo_runtimes_death_is_not_shared():
    """The behaviour-preservation case: one tenant, so the death is that
    session's own and every consumer charges it exactly as before."""
    rt = _runtime(sessions=1)
    rt._mark_dead(rt._exit_reason(-15))
    death = runtime_death.death_of(rt)
    assert death is not None
    assert death.leases == 0
    assert death.acp_sessions == 1
    assert death.shared is False
    assert runtime_death.caused_by_this_session(rt) is True


def test_a_shared_runtimes_death_is_nobodys_single_failure(caplog):
    """Two ACP sessions on one process is the sub-agent-on-parent shape, and it
    is real at cap 1 -- a sub-agent holds no lease, so the registry cannot see
    it and only the runtime's own session count can."""
    rt = _runtime(sessions=2)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.runtime_death"):
        rt._mark_dead(rt._exit_reason(-15))
    death = runtime_death.death_of(rt)
    assert death is not None
    assert death.acp_sessions == 2
    assert death.shared is True
    assert runtime_death.caused_by_this_session(rt) is False
    # The one line that says a single process ending is about to surface as
    # several sessions failing. WARNING, because the gateway runs at WARNING.
    assert "runtime_death SHARED" in caplog.text
    assert "acp_sessions=2" in caplog.text


def test_co_tenancy_is_read_before_the_dead_flag_flips():
    """MUTATION TARGET, and the reason both readings are taken in one place.

    ``outstanding_leases`` deliberately excludes a DEAD runtime's leases -- death
    releases ownership, so the kill gate can only defend a live process -- and
    ``is_alive()`` consults ``_dead``. So a count taken one line later reports
    every shared process as single-tenant and silently disables the attribution.

    Real leases through the real registry, not a patched count: the ordering only
    bites through that liveness check, so a stubbed reader would pass either way.
    """
    runtime_ownership._reset_for_tests()
    rt = _runtime(sessions=2, rc=None)

    async def _spawn() -> AcpRuntime:
        return rt

    async def _lease_twice() -> None:
        await runtime_ownership.RUNTIME_OWNERSHIP.acquire("k", "chat-1", _spawn, cap=2)
        await runtime_ownership.RUNTIME_OWNERSHIP.acquire("k", "chat-2", _spawn, cap=2)

    asyncio.run(_lease_twice())
    assert runtime_ownership.outstanding_leases(rt) == 2, "setup: two live leases expected"

    rt._mark_dead(rt._exit_reason(None))
    death = runtime_death.death_of(rt)
    assert death is not None
    assert death.leases == 2, "the lease count was taken after _dead was set"
    assert death.acp_sessions == 2
    assert death.shared is True


def test_the_death_is_announced_exactly_once():
    """N tenants must read ONE record. ``_mark_dead`` is re-entered from several
    paths (a kill, then the reader loop seeing EOF), and a second announcement
    would overwrite the first with a count taken after the process was already
    gone."""
    rt = _runtime(sessions=2)
    rt._mark_dead(rt._exit_reason(-15))
    first = runtime_death.death_of(rt)
    rt._mark_dead("a later path noticing the same death")
    assert runtime_death.death_of(rt) is first


def test_a_tenant_asks_by_handle_never_by_pid():
    """A session holds a provider, not a pid -- a session that can name a pid
    can signal it. Both provider shapes resolve to the same record."""
    rt = _runtime(sessions=2)
    rt._mark_dead(rt._exit_reason(-15))
    death = runtime_death.death_of(rt)
    inner = SimpleNamespace(_runtime=rt)
    outer = SimpleNamespace(_client=inner)
    assert runtime_death.death_of(inner) is death
    assert runtime_death.death_of(outer) is death
    assert runtime_death.caused_by_this_session(outer) is False


def test_an_unattributable_death_is_charged_as_before():
    """True is the conservative answer: a caller with no record, or a provider
    with no runtime behind it, charges exactly as it does today."""
    assert runtime_death.caused_by_this_session(None) is True
    assert runtime_death.caused_by_this_session(SimpleNamespace()) is True
    assert runtime_death.caused_by_this_session(SimpleNamespace(_client=None)) is True


def test_a_lease_only_tenant_counts_too(monkeypatch):
    """The other half of the two readings: a session that holds a lease but has
    not opened its ACP session yet is a real tenant the runtime cannot see."""
    rt = _runtime(sessions=1)
    monkeypatch.setattr("kiro_crew.acp.runtime.outstanding_leases", lambda _t: 2)
    rt._mark_dead(rt._exit_reason(-15))
    death = runtime_death.death_of(rt)
    assert death is not None
    assert death.leases == 2
    assert death.shared is True


# ───────────────── the shared-death streak bounds recovery (item b) ─────────────────


def test_shared_deaths_are_counted_somewhere_so_recovery_stays_bounded():
    """Not charging a session is not the same as retrying forever. The attempts
    are counted against the thing that is actually failing."""
    assert runtime_death.shared_deaths("chat-1") == 0
    assert runtime_death.note_shared_death("chat-1") == 1
    assert runtime_death.note_shared_death("chat-1") == 2
    assert runtime_death.shared_deaths("chat-1") == 2
    assert runtime_death.shared_deaths("chat-2") == 0


def test_a_landed_turn_clears_the_streak():
    """A completed turn proves recovery worked, so the next shared death starts
    its own count instead of inheriting one."""
    runtime_death.note_shared_death("chat-1")
    runtime_death.clear_shared_deaths("chat-1")
    assert runtime_death.shared_deaths("chat-1") == 0


def test_the_streak_store_is_bounded():
    """A session that never lands again would otherwise leave its row forever."""
    for n in range(runtime_death._SHARED_STREAK_MAX_KEYS + 50):
        runtime_death.note_shared_death(f"chat-{n}")
    assert len(runtime_death._shared_streaks) <= runtime_death._SHARED_STREAK_MAX_KEYS


# ────────────── the guards exist at the sites that charge (items b, c) ──────────────

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

# Each entry: the file, the call that must not run unguarded, and how many such
# calls the guard is expected to cover. Structural rather than behavioural
# because these sites sit inside long turn-handlers whose live paths need a real
# runtime, a real session manager and a real channel client -- while what the
# fix actually asserts is a REACHABILITY fact about the call graph, which is
# exactly what an AST can answer and a mock cannot.
_GUARDED_CHARGES = [
    ("dashboard/chat_runner.py", "_acp_pipe_death_retries", 3),
    ("slack/handler.py", "record_failure", 1),
    ("slack/gateway.py", "reset", 2),
    ("task_executor.py", "recoveries", 1),
]


def _guard_names_in_ancestors(tree: ast.AST, target: ast.AST) -> set[str]:
    """Every name tested by an ``if`` that encloses *target*."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    names: set[str] = set()
    node: ast.AST | None = target
    while node is not None:
        parent = parents.get(node)
        if isinstance(parent, ast.If):
            for sub in ast.walk(parent.test):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
        node = parent
    return names


def test_the_attribution_predicate_reaches_every_charging_site():
    """One guard, every site. A charge added later without asking whose failure
    it was is the exact regression this whole change exists to remove, and it
    would otherwise be invisible until a shared process died in production.
    """
    found = 0
    for rel, marker, _ in _GUARDED_CHARGES:
        src = (_SRC / rel).read_text()
        assert "caused_by_this_session" in src, f"{rel} never asks whose failure it was"
        found += src.count("caused_by_this_session")
    # Control: a bare count could pass on a file that merely mentions the name in
    # a comment, so require more call sites than files.
    assert found >= len(_GUARDED_CHARGES) + 3, f"only {found} attribution reads across the sites"


def _predicate_backed_guards(tree: ast.AST) -> set[str]:
    """Names bound anywhere in *tree* from a ``caused_by_this_session`` call.

    A charge site guarded by ``if _own_fault:`` is only really guarded when
    ``_own_fault`` came FROM the predicate. Counting the predicate's occurrences
    in the file cannot see that: replacing the assignment with ``= True`` leaves
    every other occurrence in place, so the count still passes while the site it
    was meant to protect charges unconditionally.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        calls = [
            c
            for c in ast.walk(node.value)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "caused_by_this_session"
        ]
        if not calls:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound.add(target.id)
    return bound


def _predicate_argument_names(tree: ast.AST) -> set[str]:
    """Names passed as the subject of a ``caused_by_this_session`` call."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "caused_by_this_session"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            names.add(node.args[0].id)
    return names


def _names_ever_given_a_real_value(tree: ast.AST) -> set[str]:
    """Names assigned something other than a bare ``None`` somewhere in *tree*."""
    real: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                real.add(t.id)
    return real


def test_the_guards_subject_is_a_provider_the_turn_actually_obtained():
    """A guard that asks about a variable nothing ever fills is not a guard.

    ``caused_by_this_session`` answers True for an unknown subject, on purpose --
    that is what keeps an unattributable death charged exactly as before. The cost
    is that dropping the ONE line that captures the provider turns every guard
    into "always charge" while every guard still reads as present, which no
    structural check on the guard alone can see. So each subject name must also be
    assigned a real value somewhere, not only initialised to ``None``.
    """
    checked = 0
    dead: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        src = path.read_text()
        if "caused_by_this_session" not in src:
            continue
        tree = ast.parse(src)
        subjects = _predicate_argument_names(tree)
        if not subjects:
            continue
        real = _names_ever_given_a_real_value(tree)
        for name in sorted(subjects):
            checked += 1
            # A parameter is filled by the caller, so it needs no assignment here.
            params = {
                a.arg
                for fn in ast.walk(tree)
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                for a in list(fn.args.args) + list(fn.args.kwonlyargs)
            }
            if name in real or name in params:
                continue
            dead.append(f"{path.name}:{name}")
    assert checked >= 4, f"only {checked} guard subjects found -- scan is broken"
    assert not dead, f"ownership guard asks about a name nothing ever fills: {dead}"


def test_the_shared_streak_is_never_bound_as_a_budget():
    """A skipped charge must not REFUND one already paid.

    ``note_shared_death`` returns its streak for logging, and assigning that
    return into the counter the own-fault branch increments is how a shared death
    hands back budget the session already spent: two own-fault deaths then one
    shared read as 1, and the next own-fault death is still under the limit --
    replaying work that is not idempotent.

    So the call is made for its EFFECT and the streak is read back with
    ``shared_deaths``; the limit is then tested against whichever count is further
    along. Structural, because the arithmetic lives inside long turn handlers
    whose live paths need a real runtime, a real session manager and a real
    channel client.
    """
    calls = 0
    bound: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        src = path.read_text()
        if "note_shared_death" not in src:
            continue
        tree = ast.parse(src)
        # The names a charge is accumulated INTO. Binding the streak to a fresh
        # local for a log line or a threshold test is fine; binding it to one of
        # THESE is the refund.
        charged = {
            t.attr if isinstance(t, ast.Attribute) else t.id
            for n in ast.walk(tree)
            if isinstance(n, ast.AugAssign)
            for t in [n.target]
            if isinstance(t, (ast.Name, ast.Attribute))
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            value = node.value
            if value is None:
                continue
            if not any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "note_shared_death"
                for sub in ast.walk(value)
            ):
                continue
            targets = [node.target] if not isinstance(node, ast.Assign) else node.targets
            for tgt in targets:
                name = (
                    tgt.attr
                    if isinstance(tgt, ast.Attribute)
                    else tgt.id if isinstance(tgt, ast.Name) else ""
                )
                if name in charged:
                    bound.append(f"{path.name}:{node.lineno} -> {name}")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "note_shared_death"
            ):
                calls += 1
    assert calls >= 4, f"only {calls} note_shared_death calls found -- scan is broken"
    assert not bound, f"shared streak bound as a budget instead of counted: {bound}"


def test_no_skipped_charge_is_left_unbounded():
    """Every exemption has a substitute bound, or a permanently dying runtime
    means a session that never gives up.

    Declining to charge is only half a decision: the counter being skipped is the
    only thing that stops the retry, the circuit breaker or the auto-pause. So any
    file that asks the ownership predicate must also count the shared streak, and
    any file that reads the streak must compare it against a limit.

    This is the invariant a reviewer had to point out twice -- once for the cron
    path, whose exemption had no bound at all and would have re-fired a job on a
    dying runtime on every tick forever.
    """
    missing: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        src = path.read_text()
        if "caused_by_this_session" not in src:
            continue
        if "note_shared_death" not in src:
            missing.append(f"{path.name}: asks whose fault, never bounds the exemption")
    assert not missing, "; ".join(missing)


def test_predicate_backed_guards_helper_is_not_vacuous():
    """Control for the two AST helpers: they must actually resolve a name.

    Both structural tests above intersect a guard-name set with an
    assignment-derived set. If either helper silently returned an empty set every
    assertion built on it would pass while checking nothing, which is the failure
    mode a structural test hides best.
    """
    tree = ast.parse(
        "import x\n"
        "def f(p):\n"
        "    own = x.caused_by_this_session(p)\n"
        "    if own:\n"
        "        c += 1\n"
    )
    assert _predicate_backed_guards(tree) == {"own"}
    assert _predicate_argument_names(tree) == {"p"}
    assert "own" in _names_ever_given_a_real_value(tree)


def test_a_substitute_bound_is_cleared_wherever_its_real_counter_is():
    """THE invariant three rounds of review kept finding new sites of.

    A shared-death streak stands in for a real counter -- a slot's recovery
    budget, a session's consecutive failures, a job's auto-pause count -- and it
    only works if it shares that counter's whole lifecycle. Two opposite failures
    come from getting this wrong, and both shipped once:

    * a key that is FRESHER than the counter (a non-persistent cron's
      ``cron:{id}:{uuid}``) never accumulates, so the bound never fires and the
      exemption is unbounded;
    * a key never CLEARED on success accumulates for the process's life, so the
      bound is permanently tripped, the exemption silently stops applying, and the
      log reports a lifetime total as a consecutive one.

    So: any file that counts a streak must also clear one. Checked as a pair,
    because the two calls are what make the streak consecutive rather than
    cumulative.
    """
    counts: list[str] = []
    clears: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        src = path.read_text()
        if "note_shared_death" in src:
            counts.append(path.name)
        if "clear_shared_deaths" in src:
            clears.append(path.name)
    assert len(counts) >= 4, f"only {counts} count a streak -- scan is broken"
    missing = sorted(set(counts) - set(clears))
    assert not missing, f"counts a shared-death streak but never clears one: {missing}"


def test_the_cron_streak_is_keyed_to_the_job_not_the_run():
    """A non-persistent cron's session key is ``cron:{id}:{uuid}``, minted fresh
    per run, while the counter the streak substitutes for lives on the JOB. Keyed
    to the run, the streak could never exceed 1 and auto-pause would be disabled
    for exactly the jobs whose runtime keeps dying.
    """
    src = (_SRC / "slack" / "gateway.py").read_text()
    tree = ast.parse(src)
    subjects: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"note_shared_death", "clear_shared_deaths"}
            and node.args
        ):
            subjects.append(ast.unparse(node.args[0]))
    assert subjects, "no shared-death call found in the cron path -- scan is broken"
    for got in subjects:
        assert "job.id" in got, f"cron shared-death streak keyed to {got!r}, not the job"
        assert "session_key" not in got, f"cron streak keyed to a per-run key: {got!r}"


def test_every_death_handler_that_charges_a_budget_asks_whose_fault_it_was():
    """THE invariant, stated once and checked everywhere.

    A counter advanced inside an ``except AcpProcessDied`` handler is a bill sent
    to one session for a PROCESS event. Every such charge must sit under a
    condition that came from the ownership predicate -- not merely under some
    boolean named as though it had, which is why the guard name is traced back to
    its binding.

    Written over the whole handler population rather than one file on purpose. The
    first version of this change guarded three of the four sites and an external
    reviewer found the fourth (`task_executor.py`, the taskrunner's
    ``recoveries``); a per-file test would have passed on that PR. This one fails
    on a fifth site the moment somebody adds it.

    A reset to zero is not a charge and is excluded: zeroing a budget is the
    conservative direction and needs no ownership question.
    """
    handlers = 0
    unguarded: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        backed = _predicate_backed_guards(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = node.type
            names = (
                {s.id for s in ast.walk(caught) if isinstance(s, ast.Name)}
                if caught is not None
                else set()
            )
            if "AcpProcessDied" not in names:
                continue
            handlers += 1
            for sub in ast.walk(node):
                if not isinstance(sub, ast.AugAssign):
                    continue
                if not isinstance(sub.op, ast.Add):
                    continue
                if not (_guard_names_in_ancestors(tree, sub) & backed):
                    unguarded.append(f"{path.name}:{sub.lineno}")
    # Control: the scan must actually find the handler population. Zero handlers
    # would make the assertion below vacuously true, which is the failure mode a
    # structural test is most likely to hide.
    assert handlers >= 4, f"only {handlers} AcpProcessDied handlers found -- scan is broken"
    assert not unguarded, f"death handler charges a budget with no ownership guard: {unguarded}"


def test_every_retry_budget_charge_is_guarded_by_the_predicate_itself():
    """The same rule for the chat runner's budget specifically, including the
    arms that catch a pipe death as a plain ``AcpError`` rather than a typed one
    and so fall outside the handler scan above.
    """
    tree = ast.parse((_SRC / "dashboard" / "chat_runner.py").read_text())
    backed = _predicate_backed_guards(tree)
    assert backed, "no guard name is bound from caused_by_this_session"
    unguarded: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AugAssign):
            continue
        target = node.target
        if not (isinstance(target, ast.Attribute) and target.attr == "_acp_pipe_death_retries"):
            continue
        if not (_guard_names_in_ancestors(tree, node) & backed):
            unguarded.append(node.lineno)
    assert not unguarded, f"retry budget charged with no ownership guard: lines {unguarded}"


def test_the_subagent_death_arms_guard_their_parent_reset():
    """MUTATION TARGET for item (c). A sub-agent runs on its parent's process,
    so the death arms catch the CHILD's death too -- and an unguarded
    ``reset(parent_key)`` there tears down a healthy parent conversation, and
    every co-tenant session with it, over a child that failed.
    """
    tree = ast.parse((_SRC / "slack" / "gateway.py").read_text())
    unguarded: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "reset"):
            continue
        if not any(isinstance(a, ast.Name) and a.id == "parent_key" for a in node.args):
            continue
        handler = _enclosing_death_handler(tree, node)
        if handler is None:
            continue
        if "caused_by_this_session" not in _guard_names_in_ancestors(tree, node):
            unguarded.append(node.lineno)
    assert not unguarded, f"parent reset on a death arm with no ownership guard: lines {unguarded}"


def _enclosing_death_handler(tree: ast.AST, target: ast.AST) -> ast.ExceptHandler | None:
    """The ``except`` arm for a PROCESS DEATH that encloses *target*, if any.

    Only the death arms are in scope. An injection TIMEOUT also resets the
    parent, and guarding that on a death record would be wrong: there is no
    death, so the record is absent and the guard would answer "charge it"
    anyway -- while reading as though the site had been considered.
    """
    deaths = {"AcpProcessDied", "PromptBusyExhaustedError"}
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    node: ast.AST | None = target
    while node is not None:
        if isinstance(node, ast.ExceptHandler):
            caught = node.type
            names = (
                {sub.id for sub in ast.walk(caught) if isinstance(sub, ast.Name)}
                if caught is not None
                else set()
            )
            if names & deaths:
                return node
        node = parents.get(node)
    return None
