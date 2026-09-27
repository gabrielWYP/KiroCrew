#!/usr/bin/env python3
"""EXACT tool list codex offers the model under a given evaluator config.

Model-free: codex is pointed at the scripted fake model on 127.0.0.1, which
records the Responses request (including "tools"). Runs `codex exec` once per
config variant in a fresh mktemp -d CODEX_HOME. Output: JSON on stdout.

Usage: tool_surface.py <codex-bin> <roots> label=template.toml [label=template.toml ...]
       (a template may carry a suffix '+key=value' lines appended to [features],
        e.g. 'a3-codemode=codex-home-evaluator/config.toml+code_mode_host=true')
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codex_fake_home import FakeModel, render_config, tool_names  # noqa: E402

codex, roots = sys.argv[1], sys.argv[2]
result = {}
for spec in sys.argv[3:]:
    label, rest = spec.split("=", 1)
    parts = rest.split("+")
    template, overrides = parts[0], parts[1:]
    w = tempfile.mkdtemp(prefix="tool-surface-")
    fm = FakeModel(w)
    try:
        home = os.path.join(w, "home")
        cfg = render_config(template, home, roots, fake_port=fm.port)
        if overrides:
            with open(cfg) as f:
                txt = f.read()
            for ov in overrides:
                k, v = ov.split("=", 1)
                txt = txt.replace("\n%s = " % k, "\n#A3-overridden %s = " % k)
                txt = txt.replace("[features]\n", "[features]\n%s = %s\n" % (k, v), 1)
            with open(cfg, "w") as f:
                f.write(txt)
        cwd = os.path.join(w, "wit")
        os.makedirs(cwd)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "OPENAI_"))}
        env["CODEX_HOME"] = home
        p = subprocess.run([codex, "exec", "--skip-git-repo-check", "-C", cwd, "list the tools you have"],
                           capture_output=True, text=True, env=env, timeout=120)
        reqs = fm.requests()
        first = reqs[0] if reqs else {}
        result[label] = {
            "template": template, "overrides": overrides, "exit": p.returncode,
            "model_requests": len(reqs),
            "tools": sorted(tool_names(first)) if first else None,
            "stderr_tail": p.stderr[-600:] if not reqs else "",
        }
    finally:
        fm.close()
print(json.dumps(result, indent=1))
