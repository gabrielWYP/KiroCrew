#!/usr/bin/env python3
"""The mcpServers array KiroCrew REALLY sends in session/new for a codex agent.

Calls the exact projection AcpRuntime._mirrored_session_mcp uses for a codex
runtime (providers.mirrors.registry.mirror_for("codex").session_projection),
with the real agent specs under ~/.kiro/agents (read only), twice per agent:
  * stubs=[]    -> no shared MCP gateway;
  * stubs=pooled -> the managed control-plane servers (kirocrew-core/-cron)
    offered as pooled gateway stubs, the way the gateway offers them.
Prints one JSON line per (agent, stubs) with every server the adapter would
receive, and whether each one can write (by name: every server except ro_fs
is treated as write-capable, i.e. a finding).

Usage: <kirocrew-python> mcp_surface.py [--work-dir DIR] [agent ...]
"""
import argparse
import json

from kiro_crew.agent import managed_mcp_spec_entry
from kiro_crew.providers.mirrors.registry import mirror_for

ap = argparse.ArgumentParser()
ap.add_argument("--work-dir", default=None)
ap.add_argument("agents", nargs="*")
a = ap.parse_args()
agents = a.agents or ["codex-evaluator"]

stubs = []
for n in ("kirocrew-core", "kirocrew-cron"):
    e = managed_mcp_spec_entry(n)
    if e:
        stubs.append({"name": n, **{k: v for k, v in e.items() if k in ("command", "args", "env", "type")}})

m = mirror_for("codex")
for agent in agents:
    for label, st in (("no-stubs", []), ("pooled-stubs", stubs)):
        p = m.session_projection(agent or None, stub_server_names=frozenset(s["name"] for s in st),
                                 stub_elements=st, work_dir=a.work_dir, session_key="mcp-surface-probe",
                                 channel_id="", session_token="probe")
        servers = p.params.get("mcpServers") or []
        print(json.dumps({
            "agent": agent, "stubs": label, "offered": [s["name"] for s in st],
            "mcpServers": [{"name": s.get("name"), "command": s.get("command"),
                            "args": (s.get("args") or [])[:4], "type": s.get("type"),
                            "env_keys": sorted((s.get("env") or {}) if isinstance(s.get("env"), dict)
                                               else [e.get("name") for e in s.get("env") or []])}
                           for s in servers],
            "write_capable": [s.get("name") for s in servers if s.get("name") != "ro_fs"],
            "denied_tools": sorted(map(list, p.denied_tools))[:20],
        }))
