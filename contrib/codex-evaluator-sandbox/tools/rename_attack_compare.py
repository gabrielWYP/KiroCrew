#!/usr/bin/env python3
"""Run the rename/TOCTOU attacks of tests/rename_attacks.py against A2 (both
confinement modes) and A3, each in a fresh mktemp -d tree. Output: JSONL.

Usage: rename_attack_compare.py <A2 ro_fs_mcp.py> <A3 ro_fs_mcp.py> [seconds]
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
import rename_attacks as RA  # noqa: E402

a2, a3 = sys.argv[1], sys.argv[2]
secs = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0


def load(src, name):
    spec = importlib.util.spec_from_file_location(name, src)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


IMPLS = [("A2-openat2", a2, {}), ("A2-walk", a2, {"walk": True}),
         ("A3", a3, {}), ("A3-without-revalidation", a3, {"noreval": True})]
ATTACKS = [
    ("grep_subdir_moved_before", lambda m, r, o, nr: RA.attack_grep_subdir_moved(m, r, o, "before", nr)),
    ("grep_subdir_moved_after", lambda m, r, o, nr: RA.attack_grep_subdir_moved(m, r, o, "after", nr)),
    ("grep_ancestor_moved_before", lambda m, r, o, nr: RA.attack_grep_ancestor_moved(m, r, o, "before", nr)),
    ("grep_ancestor_moved_after", lambda m, r, o, nr: RA.attack_grep_ancestor_moved(m, r, o, "after", nr)),
    ("list_dir_moved", lambda m, r, o, nr: RA.attack_list_moved(m, r, o)),
    ("read_ancestor_moved", lambda m, r, o, nr: RA.attack_read_ancestor_moved(m, r, o)),
    ("swap_race", lambda m, r, o, nr: RA.attack_swap_race(m, r, o, secs)),
]
for label, src, opt in IMPLS:
    for aname, fn in ATTACKS:
        if opt.get("noreval") and aname.startswith(("list_dir", "read_")):
            continue  # those two are closed BY the revalidation; nothing to show without it
        tmp = tempfile.mkdtemp(prefix="rename-cmp-")
        base = os.path.realpath(tmp)
        root, outside = os.path.join(base, "root"), os.path.join(base, "outside")
        os.makedirs(root)
        os.makedirs(outside)
        m = load(src, "m_%s_%s" % (label.replace("-", "_"), aname))
        (m.ALLOW_EXCEPTIONS if hasattr(m, "ALLOW_EXCEPTIONS") else m.KIRO_EXCEPTIONS).append(base)
        m.load_roots(root)
        if opt.get("walk"):
            m.USE_OPENAT2 = False
        confinement = "openat2" if getattr(m, "USE_OPENAT2", getattr(m, "OPENAT2_OK", False)) else "openat-walk"
        try:
            res = fn(m, root, outside, bool(opt.get("noreval")))
        finally:
            for _p, fd in m.ROOTS:
                os.close(fd)
            shutil.rmtree(tmp, ignore_errors=True)
        res.update(impl=label, confinement=confinement)
        if not res.get("leaked"):
            res.pop("output", None)
        print(json.dumps(res), flush=True)
