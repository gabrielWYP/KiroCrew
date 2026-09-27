#!/usr/bin/env python3
"""Per-session vs per-runtime lock: same scenarios against whatever kiro_crew is
on PYTHONPATH (A2 copy or A3 copy). Real AcpRuntime.create_session + real
reader loop + real pipe to a minimal ACP adapter; the answer is read from what
the adapter received. Output: JSON lines.

Usage: PYTHONPATH=<copy> <kirocrew-python> lock_compare.py <label>
"""
import asyncio
import json
import os
import sys
import tempfile
import textwrap

import kiro_crew
from kiro_crew.acp.runtime import AcpRuntime

LABEL = sys.argv[1]
FAKE_ACP = textwrap.dedent(r'''
    import json, sys
    log = open(sys.argv[1], "a")
    n = [0]
    def out(o):
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
    cfg = [{"id": "mode", "name": "Mode", "type": "select", "currentValue": "agent",
            "options": [{"value": "read-only", "name": "Read only"}, {"value": "agent", "name": "Agent"}]}]
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
            if meth == "session/new":
                n[0] += 1
                out({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": "S-%d" % n[0], "configOptions": cfg}})
            else:
                out({"jsonrpc": "2.0", "id": m["id"], "result": {}})
''')


async def scenario(name, rt_agent, sessions, rekey=None):
    d = tempfile.mkdtemp(prefix="lockcmp-")
    log = os.path.join(d, "adapter.jsonl")
    script = os.path.join(d, "adapter.py")
    open(script, "w").write(FAKE_ACP)
    proc = await asyncio.create_subprocess_exec(sys.executable, script, log, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE)
    rt = AcpRuntime(agent=rt_agent, acp_backend="codex", work_dir=d)
    rt._process, rt._dead, rt._initialized = proc, False, True
    if hasattr(rt, "_codex_role"):
        rt._codex_role = "evaluator"  # as if spawned with the evaluator CODEX_HOME
    reader = asyncio.ensure_future(rt._reader_loop())
    out = {"impl": LABEL, "scenario": name, "runtime_agent": rt_agent, "results": {}}
    try:
        rid = 500
        for agent in sessions:
            try:
                h = await rt.create_session(cwd=d, agent=agent)
            except Exception as e:
                out["results"][agent] = "create refused: %s" % type(e).__name__
                continue
            if rekey:
                from kiro_crew.acp.session_provider import AcpSessionProvider
                AcpSessionProvider(h, rt, session_key="k").rekey("k2", crew_agent=rekey)
            rid += 1
            await rt.send_notification("a3/emit", {"sessionId": h._session_id, "id": rid})
            msg = await asyncio.wait_for(h._queue.get(), 10)
            h._build_permission_event(msg)
            await h.approve_tool(rid, option_id="allow_always")   # what parent_policy=auto does
            ans = None
            for _ in range(200):
                for l in open(log):
                    fr = json.loads(l)
                    if fr.get("id") == rid and "method" not in fr:
                        ans = fr.get("result")
                if ans is not None:
                    break
                await asyncio.sleep(0.05)
            o = (ans or {}).get("outcome") or {}
            out["results"][agent] = o.get("optionId") or o.get("outcome")
    finally:
        proc.kill()
        await proc.wait()
        reader.cancel()
    print(json.dumps(out), flush=True)


async def main():
    await scenario("generic runtime hosts evaluator + proposer siblings", "kirocrew",
                   ["codex-evaluator", "codex-proposer"])
    await scenario("proposer-owned runtime hosts an evaluator session", "codex-proposer",
                   ["codex-evaluator", "codex-proposer"])
    await scenario("evaluator-spawned runtime hosts a proposer session", "codex-evaluator",
                   ["codex-proposer"])
    await scenario("warm-pool session rekeyed to a crew identity", "codex-evaluator", [None], rekey="some-crew")


print(json.dumps({"kiro_crew": kiro_crew.__file__}), file=sys.stderr)
asyncio.run(main())
