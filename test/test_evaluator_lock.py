"""Tests for the codex-sandbox A3 KiroCrew changes (per-session lock + CODEX_HOME by role).

Run against a PATCHED COPY of kiro_crew (never the installed one):
    PYTHONPATH=<copy-dir> <kirocrew-python> -m unittest -v tests.test_evaluator_lock
tests/run_kirocrew_tests.sh builds that copy from scratch and does exactly that.
"""
import asyncio
import hashlib
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import kiro_crew


from kiro_crew.acp import evaluator_lock as L  # noqa: E402
from kiro_crew.acp.client import AcpClient  # noqa: E402
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError  # noqa: E402
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings  # noqa: E402
from kiro_crew.acp.session_provider import AcpSessionProvider  # noqa: E402
from kiro_crew.acp.types import JsonRpcMessage  # noqa: E402
from kiro_crew import subagent as subagent_mod  # noqa: E402
from kiro_crew.subagent_manager import run as run_mod  # noqa: E402

ALLOW = {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
CODEX_OPTS = [{"optionId": "allow_once", "name": "Yes", "kind": "allow_once"},
              {"optionId": "allow_for_session", "name": "Yes, session", "kind": "allow_always"},
              {"optionId": "decline", "name": "No", "kind": "reject_once"}]
ALIAS_EVAL = "kirocrew-skill-view-" + "a" * 24
ALIAS_PROP = "kirocrew-skill-view-" + "b" * 24
ALIAS_BAD = "kirocrew-skill-view-" + "c" * 24


class FakeStdin:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(json.loads(data.decode()))

    async def drain(self):
        pass


def fake_process():
    return SimpleNamespace(stdin=FakeStdin(), returncode=None)


def perm_frame(rid, sid):
    return JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
                                     "params": {"sessionId": sid, "options": CODEX_OPTS,
                                                "toolCall": {"title": "require_escalated: rm -rf x",
                                                             "kind": "execute"}}})


class AgentsDirMixin:
    """Scratch ~/.kiro/agents with skill-view aliases + sidecars (real format)."""

    def setUp(self):
        super().setUp()
        self.agents = Path(tempfile.mkdtemp(prefix="a3-agents-"))
        side = self.agents / ".kirocrew-skill-projection-metadata"
        side.mkdir()
        for alias, agent, good_sha in ((ALIAS_EVAL, "codex-evaluator", True),
                                       (ALIAS_PROP, "codex-proposer", True),
                                       (ALIAS_BAD, "codex-evaluator", False)):
            spec = json.dumps({"name": alias, "acp_backend": "codex", "tools": ["fs_read"]})
            (self.agents / f"{alias}.json").write_text(spec)
            sha = hashlib.sha256(spec.encode()).hexdigest() if good_sha else "0" * 64
            (side / f"{alias}.json").write_text(json.dumps({
                "x-kirocrew-managed": "skill-view", "x-kirocrew-agent": agent,
                "x-kirocrew-alias-sha256": sha}))
        self._p = mock.patch.object(L, "agents_dir_override", self.agents)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        super().tearDown()


# ---------------------------------------------------------------- policy
class TestPolicy(AgentsDirMixin, unittest.TestCase):
    def test_matrix(self):
        locked = [
            ("codex", "codex-evaluator"), ("kiro", "codex-evaluator"), ("claude", "codex-evaluator-2"),
            ("codex", "codex-evaluator-ronda.2"), ("codex", "some-evaluator"), ("codex", ""),
            ("codex", None), ("codex", ALIAS_EVAL), ("kiro", ALIAS_EVAL),   # alias resolved -> evaluator
            ("codex", ALIAS_BAD),                                           # sha mismatch -> unresolved
            ("codex", "kirocrew-skill-view-" + "d" * 24),                   # no sidecar -> unresolved
        ]
        for backend, name in locked:
            self.assertIsNotNone(L.permission_lock_reason(backend, name), (backend, name))
        free = [
            ("codex", "kirocrew"),          # A3: Crew's default agent is a positive identity
            ("codex", "codex-proposer"), ("codex", ALIAS_PROP), ("kiro", "kirocrew"), ("kiro", ""),
            ("kiro", ALIAS_BAD),            # unresolved alias only fails closed on codex
            ("claude", "claude-proposer"), ("kiro", "codex-evaluatorX"),
        ]
        for backend, name in free:
            self.assertIsNone(L.permission_lock_reason(backend, name), (backend, name))

    def test_alias_resolution_sources(self):
        self.assertEqual(L.resolve_alias(ALIAS_EVAL), "codex-evaluator")
        self.assertIsNone(L.resolve_alias(ALIAS_BAD))
        self.assertIsNone(L.resolve_alias("kirocrew-skill-view-../../etc"))
        # in-process projection registry wins even without a sidecar
        from kiro_crew.acp import skill_projection as sp
        alias = "kirocrew-skill-view-" + "e" * 24
        proj = sp.NativeSkillProjection(aliases={"codex-evaluator": alias})
        sp._register_active_projection(proj)
        self.assertEqual(L.resolve_alias(alias), "codex-evaluator")
        self.assertTrue(L.is_evaluator_agent(alias))

    def test_sidecar_symlink_refused(self):
        side = self.agents / ".kirocrew-skill-projection-metadata"
        alias = "kirocrew-skill-view-" + "f" * 24
        (self.agents / f"{alias}.json").write_text("{}")
        os.symlink(side / f"{ALIAS_EVAL}.json", side / f"{alias}.json")
        self.assertIsNone(L.resolve_alias(alias))

    def test_env_is_additive_only(self):
        with mock.patch.dict(os.environ, {"KIROCREW_PERMISSION_LOCK_AGENTS": "gpt-judge.*"}):
            self.assertIsNotNone(L.permission_lock_reason("kiro", "gpt-judge-1"))
            self.assertIsNotNone(L.permission_lock_reason("codex", "codex-evaluator"))

    def test_identity_is_monotonic(self):
        ev = L.make_identity("codex", "codex-evaluator")
        self.assertIsNotNone(L.identity_lock_reason(ev.tightened("claude-proposer", "")))
        gen = L.make_identity("codex", "kirocrew")
        self.assertIsNone(L.identity_lock_reason(gen))
        self.assertIsNotNone(L.identity_lock_reason(gen.tightened("codex-evaluator-x")))
        with self.assertRaises(Exception):
            ev.names = ()  # frozen

    def test_is_allow_outcome(self):
        self.assertTrue(L.is_allow_outcome(ALLOW))
        for oid in ("allow_for_session", "accept_execpolicy_amendment", "allow_permissions_turn", "weird"):
            self.assertTrue(L.is_allow_outcome({"outcome": {"outcome": "selected", "optionId": oid}}), oid)
        for oid in ("decline", "cancel", "reject_permissions", "reject_once", "deny"):
            self.assertFalse(L.is_allow_outcome({"outcome": {"outcome": "selected", "optionId": oid}}), oid)
        self.assertFalse(L.is_allow_outcome({"outcome": {"outcome": "cancelled"}}))
        self.assertFalse(L.is_allow_outcome({"token": "x"}))


# ---------------------------------------------------------------- CODEX_HOME by role
class TestCodexHomeRole(AgentsDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix="a3-evalhome-"))
        os.chmod(self.home, 0o700)
        (self.home / "config.toml").write_text('approval_policy = "never"\n')
        os.chmod(self.home / "config.toml", 0o600)
        self._e = mock.patch.dict(os.environ, {L.EVALUATOR_CODEX_HOME_ENV: str(self.home)})
        self._e.start()

    def tearDown(self):
        self._e.stop()
        super().tearDown()

    def test_evaluator_gets_its_home_and_scrubbed_env(self):
        for names in (("codex-evaluator",), (ALIAS_EVAL,), ("kirocrew", "codex-evaluator-2"), (ALIAS_BAD,)):
            env = {"CODEX_HOME": "/home/x/.codex", "CODEX_CONFIG": "{}", "INITIAL_AGENT_MODE": "agent",
                   "DISABLE_MCP_CONFIG_FILTERING": "1", "PATH": "/bin"}
            self.assertEqual(L.apply_role_codex_env(env, "codex", *names), "evaluator", names)
            self.assertEqual(env["CODEX_HOME"], str(self.home))
            for k in L.EVALUATOR_SCRUBBED_ENV:
                self.assertNotIn(k, env)
            self.assertEqual(env["CODEX_ACP_UNTRUSTED_PROJECTS"], "1")
            self.assertEqual(env["PATH"], "/bin")

    def test_other_codex_agents_never_see_the_evaluator_home(self):
        env = {"CODEX_HOME": str(self.home)}
        self.assertEqual(L.apply_role_codex_env(env, "codex", "codex-proposer"), "default")
        self.assertNotIn("CODEX_HOME", env)
        env = {"CODEX_HOME": "/opt/other-codex-home"}
        self.assertEqual(L.apply_role_codex_env(env, "codex", "kirocrew"), "default")
        self.assertEqual(env["CODEX_HOME"], "/opt/other-codex-home")
        env = {"CODEX_HOME": str(self.home)}
        self.assertEqual(L.apply_role_codex_env(env, "kiro", "codex-evaluator"), "n/a")
        self.assertEqual(env["CODEX_HOME"], str(self.home))  # non-codex: untouched

    def test_missing_or_unsafe_home_refuses_to_spawn(self):
        cases = []
        missing = self.home / "nope"
        cases.append(missing)
        grp = Path(tempfile.mkdtemp(prefix="a3-grp-"))
        (grp / "config.toml").write_text("")
        os.chmod(grp, 0o777)
        cases.append(grp)
        link = Path(tempfile.mkdtemp(prefix="a3-lnk-")) / "home"
        os.symlink(self.home, link)
        cases.append(link)
        nocfg = Path(tempfile.mkdtemp(prefix="a3-nocfg-"))
        os.chmod(nocfg, 0o700)
        cases.append(nocfg)
        for bad in cases:
            with mock.patch.dict(os.environ, {L.EVALUATOR_CODEX_HOME_ENV: str(bad)}):
                env = {"CODEX_HOME": "/home/x/.codex"}
                with self.assertRaises(L.EvaluatorSandboxError, msg=str(bad)):
                    L.apply_role_codex_env(env, "codex", "codex-evaluator")
                self.assertEqual(env["CODEX_HOME"], "/home/x/.codex")  # never half-applied

    def test_pinned_hashes(self):
        cfg = (self.home / "config.toml").read_bytes()
        (self.home / "ro_fs_mcp.py").write_text("# ro_fs\n")
        rofs = (self.home / "ro_fs_mcp.py").read_bytes()
        good = {L.EVALUATOR_CONFIG_PIN_ENV: hashlib.sha256(cfg).hexdigest(),
                L.EVALUATOR_ROFS_PIN_ENV: hashlib.sha256(rofs).hexdigest()}
        with mock.patch.dict(os.environ, good):
            self.assertEqual(L.apply_role_codex_env({}, "codex", "codex-evaluator"), "evaluator")
            (self.home / "ro_fs_mcp.py").write_text("# tampered by another agent\n")
            with self.assertRaises(L.EvaluatorSandboxError):
                L.apply_role_codex_env({}, "codex", "codex-evaluator")
        with mock.patch.dict(os.environ, {L.EVALUATOR_CONFIG_PIN_ENV: "0" * 64}):
            with self.assertRaises(L.EvaluatorSandboxError):
                L.apply_role_codex_env({}, "codex", "codex-evaluator")

    def test_acpclient_spawn_path_uses_role(self):
        # The legacy AcpClient path shares the helper: identity snapshot + role.
        c = AcpClient(agent="codex-evaluator", acp_backend="codex", work_dir=tempfile.gettempdir())
        ident = c._evaluator_lock_identity()
        env = {}
        self.assertEqual(L.apply_role_codex_env(env, c.backend, *ident.names), "evaluator")
        self.assertEqual(env["CODEX_HOME"], str(self.home))


# ---------------------------------------------------------------- runtime: per-session decisions
def make_runtime(agent="kirocrew", backend="codex"):
    r = AcpRuntime(agent=agent, acp_backend=backend, work_dir=tempfile.gettempdir())
    r._process = fake_process()
    r._dead = False
    return r


def add_session(rt, sid, agent, crew=None):
    ident = rt._register_lock_identity(sid, rt._lock_identity_for(agent, crew, "test"))
    q = asyncio.Queue()
    rt._session_queues[sid] = q
    h = AcpSessionHandle(sid, q, rt, watchdog=WatchdogSettings(),
                         crew_agent=crew if crew is not None else rt._crew_agent)
    h._lock_identity = ident
    return h


class TestRuntimeSiblings(AgentsDirMixin, unittest.IsolatedAsyncioTestCase):
    async def _check(self, rt_agent):
        rt = make_runtime(agent=rt_agent)
        ev = add_session(rt, "S-EV", "codex-evaluator")
        pr = add_session(rt, "S-PR", "codex-proposer")
        al = add_session(rt, "S-AL", ALIAS_EVAL)
        for rid, sid in ((1, "S-EV"), (2, "S-PR"), (3, "S-AL"), (4, "S-EV"), (5, "S-PR")):
            rt._note_permission_origin(perm_frame(rid, sid))
        for h, rid in ((ev, 1), (pr, 2), (al, 3)):
            h._permission_options[rid] = {"once": "allow_once", "always": "allow_for_session", "reject": "decline"}
            await h.approve_tool(rid, option_id="allow_always")
        # a caller that bypasses approve_tool: wire backstop, per EMITTING session
        await rt.send_response(4, ALLOW)
        await rt.send_response(5, ALLOW)
        got = {f["id"]: f["result"] for f in rt._process.stdin.frames}
        self.assertEqual(got[1], {"outcome": {"outcome": "selected", "optionId": "decline"}})
        self.assertEqual(got[2]["outcome"]["optionId"], "allow_for_session")   # proposer NOT blocked
        self.assertEqual(got[3]["outcome"]["optionId"], "decline")             # alias -> evaluator
        self.assertEqual(got[4], L.CANCELLED_RESULT)
        self.assertEqual(got[5], ALLOW)

    async def test_generic_runtime_hosting_siblings(self):
        await self._check("kirocrew")          # A2 blocked the proposer here

    async def test_evaluator_runtime_hosting_a_proposer_sibling(self):
        await self._check("codex-evaluator")   # A2 used the runtime identity for everyone

    async def test_unattributable_requests_fail_closed_on_codex(self):
        rt = make_runtime()
        add_session(rt, "S-PR", "codex-proposer")
        rt._note_permission_origin(perm_frame(9, "S-UNKNOWN"))
        await rt.send_response(9, ALLOW)            # recorded, but no registered session
        await rt.send_response(10, ALLOW)           # never seen at all
        self.assertEqual([f["result"] for f in rt._process.stdin.frames], [L.CANCELLED_RESULT] * 2)

    async def test_same_id_from_two_sessions_is_ambiguous(self):
        rt = make_runtime()
        pr = add_session(rt, "S-PR", "codex-proposer")
        add_session(rt, "S-EV", "codex-evaluator")
        rt._note_permission_origin(perm_frame(7, "S-PR"))
        rt._note_permission_origin(perm_frame(7, "S-EV"))
        await pr.approve_tool(7)
        self.assertFalse(L.is_allow_outcome(rt._process.stdin.frames[-1]["result"]))

    async def test_backend_child_attributed_to_owner(self):
        rt = make_runtime()
        add_session(rt, "S-EV", "codex-evaluator")
        rt._subagent_sessions = {"CHILD"}
        rt._subagent_owner = "S-EV"
        rt._note_permission_origin(perm_frame(11, "CHILD"))
        await rt.send_response(11, ALLOW)
        self.assertEqual(rt._process.stdin.frames[-1]["result"], L.CANCELLED_RESULT)

    async def test_non_codex_runtime_passthrough(self):
        rt = make_runtime(backend="kiro")
        add_session(rt, "S", "kirocrew")
        rt._note_permission_origin(perm_frame(1, "S"))
        await rt.send_response(1, ALLOW)
        await rt.send_response(2, ALLOW)  # unknown id, no locked session -> untouched
        self.assertEqual([f["result"] for f in rt._process.stdin.frames], [ALLOW, ALLOW])

    async def test_evaluator_session_refused_on_default_codex_process(self):
        rt = make_runtime()
        rt._codex_role = "default"
        with self.assertRaises(AcpRuntimeError):
            rt._refuse_locked_session_on_default_runtime(rt._lock_identity_for("codex-evaluator", None, "t"))
        rt._refuse_locked_session_on_default_runtime(rt._lock_identity_for("codex-proposer", None, "t"))
        rt._codex_role = "evaluator"
        rt._refuse_locked_session_on_default_runtime(rt._lock_identity_for("codex-evaluator", None, "t"))


class TestWarmPoolRekey(AgentsDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_rekey_cannot_unlock_and_can_lock(self):
        rt = make_runtime(agent="codex-evaluator")
        ev = add_session(rt, "S-EV", None)                  # pooled: runs the runtime's agent
        prov = AcpSessionProvider(ev, rt, session_key="k1")
        prov.rekey("k2", crew_agent="")                     # claim with no crew
        prov.rekey("k3", crew_agent="claude-proposer")      # claim by some crew
        rt._note_permission_origin(perm_frame(1, "S-EV"))
        await prov.approve_tool(1, always=True)
        self.assertEqual(rt._process.stdin.frames[-1]["result"], L.CANCELLED_RESULT)

        rt2 = make_runtime(agent="kirocrew")
        gen = add_session(rt2, "S-G", None)
        prov2 = AcpSessionProvider(gen, rt2, session_key="k1")
        rt2._note_permission_origin(perm_frame(2, "S-G"))
        await prov2.approve_tool(2)
        self.assertEqual(rt2._process.stdin.frames[-1]["result"], ALLOW)
        prov2.rekey("k2", crew_agent="codex-evaluator-7")  # tightens
        rt2._note_permission_origin(perm_frame(3, "S-G"))
        await prov2.approve_tool(3)
        self.assertEqual(rt2._process.stdin.frames[-1]["result"], L.CANCELLED_RESULT)
        self.assertIsNotNone(L.identity_lock_reason(rt2._lock_identities["S-G"]))


# ---------------------------------------------------------------- real reader loop + real pipe
FAKE_ACP = textwrap.dedent(r'''
    import json, sys
    # Minimal ACP adapter: answers requests, logs everything the client writes,
    # and on the notification "a3/emit" sends a codex-style permission request.
    log = open(sys.argv[1], "a")
    n = [0]
    def out(o):
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        log.write(json.dumps(m) + "\n"); log.flush()
        meth, p = m.get("method"), m.get("params") or {}
        if meth == "a3/emit":
            out({"jsonrpc": "2.0", "id": p["id"], "method": "session/request_permission",
                 "params": {"sessionId": p["sessionId"], "toolCall": {"title": "require_escalated: touch /x",
                            "kind": "execute"}, "options": [
                     {"optionId": "allow_once", "name": "Yes", "kind": "allow_once"},
                     {"optionId": "allow_for_session", "name": "Always", "kind": "allow_always"},
                     {"optionId": "decline", "name": "No", "kind": "reject_once"}]}})
        elif meth and "id" in m:
            # codex-acp 1.13.1 shape: the 'mode' select Crew arms to read-only.
            cfg = [{"id": "mode", "name": "Mode", "type": "select", "currentValue": "agent",
                    "options": [{"value": "read-only", "name": "Read only"},
                                {"value": "agent", "name": "Agent"}]}]
            if meth == "session/new":
                n[0] += 1
                out({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": "S-NEW-%d" % n[0],
                     "configOptions": cfg}})
            elif meth == "session/load":
                out({"jsonrpc": "2.0", "id": m["id"], "result": {"modes": {}, "configOptions": cfg}})
            else:
                out({"jsonrpc": "2.0", "id": m["id"], "result": {}})
''')


class TestRealPipe(AgentsDirMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.d = tempfile.mkdtemp(prefix="a3-pipe-")
        self.log = os.path.join(self.d, "adapter.jsonl")
        script = os.path.join(self.d, "adapter.py")
        with open(script, "w") as f:
            f.write(FAKE_ACP)
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, script, self.log, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        rt = AcpRuntime(agent="kirocrew", acp_backend="codex", work_dir=self.d)
        rt._process = self.proc
        rt._dead = False
        rt._initialized = True
        rt._can_load_session = True
        self.rt = rt
        self.reader = asyncio.ensure_future(rt._reader_loop())

    async def asyncTearDown(self):
        self.proc.kill()
        await self.proc.wait()
        self.reader.cancel()

    def frames(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    async def answer_for(self, rid, timeout=10):
        for _ in range(int(timeout / 0.05)):
            for fr in self.frames():
                if fr.get("id") == rid and "method" not in fr:
                    return fr.get("result")
            await asyncio.sleep(0.05)
        raise AssertionError("no answer for %s" % rid)

    async def emit_and_approve(self, handle, rid):
        await self.rt.send_notification("a3/emit", {"sessionId": handle._session_id, "id": rid})
        msg = await asyncio.wait_for(handle._queue.get(), 10)
        self.assertEqual(msg.id, rid)
        handle._build_permission_event(msg) if hasattr(handle, "_build_permission_event") else None
        await handle.approve_tool(rid, option_id="allow_always")
        return await self.answer_for(rid)

    async def test_siblings_through_the_real_reader(self):
        ev = add_session(self.rt, "S-EV", "codex-evaluator")
        pr = add_session(self.rt, "S-PR", "codex-proposer")
        self.assertEqual((await self.emit_and_approve(ev, 101))["outcome"]["optionId"], "decline")
        self.assertIn((await self.emit_and_approve(pr, 102))["outcome"]["optionId"],
                      ("allow_for_session", "allow_always"))
        # bypass approve_tool entirely: still cancelled for the evaluator's id
        await self.rt.send_notification("a3/emit", {"sessionId": "S-EV", "id": 103})
        await asyncio.wait_for(ev._queue.get(), 10)
        await self.rt.send_response(103, ALLOW)
        self.assertEqual(await self.answer_for(103), L.CANCELLED_RESULT)

    async def test_spawn_continue_load_session(self):
        self.rt._codex_role = "evaluator"
        h = await self.rt.load_session("", "S-RESUMED", cwd=self.d, agent="codex-evaluator")
        load = next(f for f in self.frames() if f.get("method") == "session/load")
        self.assertEqual(load["params"]["mcpServers"], [])
        self.assertIsNotNone(L.identity_lock_reason(h._lock_identity))
        self.assertEqual((await self.emit_and_approve(h, 201))["outcome"]["optionId"], "decline")

    async def test_load_session_refused_on_default_process(self):
        self.rt._codex_role = "default"
        with self.assertRaises(AcpRuntimeError):
            await self.rt.load_session("", "S-X", cwd=self.d, agent="codex-evaluator")
        self.assertFalse([f for f in self.frames() if f.get("method") == "session/load"])

    async def test_create_session_alias_evaluator_gets_no_mcp(self):
        self.rt._codex_role = "evaluator"
        h = await self.rt.create_session(cwd=self.d, agent=ALIAS_EVAL)
        new = [f for f in self.frames() if f.get("method") == "session/new"][-1]
        self.assertEqual(new["params"]["mcpServers"], [])
        self.assertEqual((await self.emit_and_approve(h, 301))["outcome"]["optionId"], "decline")


# ---------------------------------------------------------------- subagent rungs + sharing
class TestSubagentApproveAndLog(AgentsDirMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.logged = []
        fake_sel = SimpleNamespace(log_tool_invocation=lambda **kw: self.logged.append(kw))
        self.p1 = mock.patch.object(subagent_mod, "sel", lambda: fake_sel)
        self.p2 = mock.patch.object(subagent_mod, "emit_counter", lambda *a, **k: None)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        super().tearDown()

    async def _run(self, client, agent, rid, reason="parent_policy_auto"):
        ev = SimpleNamespace(title="Run command", tool_kind="execute", sub_session_id="")
        info = SimpleNamespace(agent=agent, tool_count=0)
        await subagent_mod.SubagentManager._approve_and_log(
            client, rid, "sk", ev, metadata={"reason": reason}, info=info)

    async def test_shared_runtime_siblings(self):
        rt = make_runtime(agent="kirocrew")
        ev = AcpSessionProvider(add_session(rt, "S-EV", "codex-evaluator"), rt, session_key="a")
        pr = AcpSessionProvider(add_session(rt, "S-PR", "codex-proposer"), rt, session_key="b")
        rt._note_permission_origin(perm_frame(1, "S-EV"))
        rt._note_permission_origin(perm_frame(2, "S-PR"))
        for reason in ("parent_policy_auto", "hook_auto_approve", "child_interactive_approved"):
            self.logged.clear()
            await self._run(ev, "", 1, reason)          # info.agent empty: handle identity decides
            self.assertEqual(self.logged[0]["outcome"], "denied")
            self.assertEqual(self.logged[0]["error"], L.LOCK_ERROR)
        self.logged.clear()
        await self._run(pr, "codex-proposer", 2)
        self.assertEqual(self.logged[0]["outcome"], "auto_approved")
        self.assertEqual(rt._process.stdin.frames[-1]["result"]["outcome"]["optionId"], "allow_once")

    async def test_dedicated_acpclient(self):
        c = AcpClient(agent="codex-evaluator", acp_backend="codex", work_dir=tempfile.gettempdir())
        c._process = fake_process()
        await self._run(SimpleNamespace(_client=c, backend="codex",
                                        reject_tool=c.reject_tool, approve_tool=c.approve_tool), "", 5)
        self.assertEqual(self.logged[0]["outcome"], "denied")

    async def test_mock_clients_not_locked(self):
        m = mock.MagicMock()
        m.approve_tool = mock.AsyncMock()
        await self._run(m, "claude-proposer", 1)
        m.approve_tool.assert_awaited_once()


class TestSharingDecision(AgentsDirMixin, unittest.TestCase):
    def test_evaluator_never_shares(self):
        comp = SimpleNamespace()
        fn = run_mod.__dict__
        cls = next(v for v in fn.values() if isinstance(v, type)
                   and "_should_use_session_sharing_impl" in v.__dict__)
        for agent, tpl in (("codex-evaluator", None), (ALIAS_EVAL, None), ("", "codex-evaluator-2")):
            info = SimpleNamespace(agent=agent, id="x", execution_context=SimpleNamespace(
                member_id=None, template_id=tpl))
            self.assertFalse(cls._should_use_session_sharing_impl(comp, info), (agent, tpl))


if __name__ == "__main__":
    unittest.main(verbosity=2)
