#!/usr/bin/env python3
"""Helpers: render an evaluator CODEX_HOME into a temp dir, optionally wired to
the scripted fake model (tools/fake_responses_server.py) on 127.0.0.1."""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
A3 = os.path.dirname(HERE)


def render_config(template, dest_home, roots, patch_dir=A3, fake_port=None, extra_top=""):
    os.makedirs(dest_home, exist_ok=True)
    with open(template) as f:
        txt = f.read().replace("@PATCH_DIR@", patch_dir).replace("@RO_FS_ROOTS@", roots)
    if "@EVAL_HOME@" in txt:  # A3 layout: ro_fs lives inside the CODEX_HOME
        import shutil
        shutil.copy2(os.path.join(patch_dir, "ro_fs_mcp.py"), os.path.join(dest_home, "ro_fs_mcp.py"))
        txt = txt.replace("@EVAL_HOME@", dest_home)
    if fake_port is not None:
        # Top-level keys must precede the first [table].
        top = ('model_provider = "a3fake"\nmodel = "a3-fake-model"\n'
               'model_reasoning_effort = "low"\n' + extra_top)
        txt = top + txt + ('\n[model_providers.a3fake]\nname = "a3fake"\n'
                           'base_url = "http://127.0.0.1:%d/v1"\nwire_api = "responses"\n'
                           'requires_openai_auth = false\nstream_max_retries = 0\n'
                           'request_max_retries = 0\n' % fake_port)
    os.makedirs(dest_home, exist_ok=True)
    with open(os.path.join(dest_home, "config.toml"), "w") as f:
        f.write(txt)
    return os.path.join(dest_home, "config.toml")


class FakeModel:
    def __init__(self, workdir, script=None):
        self.log = os.path.join(workdir, "fake_model.jsonl")
        self.portf = os.path.join(workdir, "fake_model.port")
        cmd = [sys.executable, os.path.join(HERE, "fake_responses_server.py"), "--log", self.log,
               "--port-file", self.portf]
        if script:
            cmd += ["--script", script]
        self.proc = subprocess.Popen(cmd)
        for _ in range(100):
            if os.path.exists(self.portf) and open(self.portf).read().strip():
                break
            time.sleep(0.05)
        self.port = int(open(self.portf).read())

    def requests(self):
        import json
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def close(self):
        self.proc.terminate()
        self.proc.wait(10)


def tool_names(request):
    out = []
    for t in (request.get("request") or {}).get("tools") or []:
        name = t.get("name") or (t.get("function") or {}).get("name") or t.get("type")
        if t.get("type") == "namespace":
            for sub in t.get("tools") or []:
                out.append("%s/%s" % (name, sub.get("name")))
        else:
            out.append(name)
    return out
