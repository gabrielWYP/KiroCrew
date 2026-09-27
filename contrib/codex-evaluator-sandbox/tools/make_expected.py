#!/usr/bin/env python3
"""Build expected.json from MEASURED files (run once when the A3 artifacts change).

Usage: make_expected.py <stock kiro_crew parent> <A3-patched kiro_crew parent>
                        <A2-patched kiro_crew parent> <stock codex-acp dir>
                        <A3 codex-acp dir> <A2 codex-acp index sha256>
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
stock_sp, a3_sp, a2_sp, acp_stock, acp_a3, a2_idx = sys.argv[1:7]
FILES = ["kiro_crew/acp/client.py", "kiro_crew/acp/session_handle.py", "kiro_crew/acp/runtime.py",
         "kiro_crew/subagent.py", "kiro_crew/subagent_manager/run.py"]


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


tmpl = os.path.join(HERE, "codex-home-evaluator", "config.toml")
feats, in_feats = [], False
for line in open(tmpl):
    s = line.strip()
    if s.startswith("["):
        in_feats = s == "[features]"
        continue
    m = re.match(r"^([a-z0-9_]+)\s*=\s*false$", s)
    if in_feats and m:
        feats.append(m.group(1))
ver = re.search(r'^__version__ = "([^"]+)"', open(os.path.join(stock_sp, "kiro_crew", "__init__.py")).read(), re.M)
exp = {
    "codex_acp_version": json.load(open(os.path.join(acp_stock, "package.json")))["version"],
    "codex_acp_index_stock": sha(os.path.join(acp_stock, "dist", "index.js")),
    "codex_acp_index_patched": sha(os.path.join(acp_a3, "dist", "index.js")),
    "codex_acp_index_A2": a2_idx,
    "kirocrew_version": ver.group(1),
    "kirocrew_orig": {f: sha(os.path.join(stock_sp, f)) for f in FILES},
    "kirocrew_patched": {f: sha(os.path.join(a3_sp, f)) for f in FILES + ["kiro_crew/acp/evaluator_lock.py"]},
    "kirocrew_A2": {f: sha(os.path.join(a2_sp, f)) for f in FILES[:4] + ["kiro_crew/acp/evaluator_lock.py"]},
    "ro_fs_mcp_sha256": sha(os.path.join(HERE, "ro_fs_mcp.py")),
    "config_template_sha256": sha(tmpl),
    "features_required_false": feats,
    "codex_cli_tested": "codex-cli 0.157.1",
}
json.dump(exp, sys.stdout, indent=2)
print()
