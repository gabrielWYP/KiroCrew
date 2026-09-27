"""Rename/TOCTOU attacks against a ro_fs_mcp implementation (A2 or A3).

Shared by tests/test_ro_fs_mcp.py (asserts A3 leaks nothing) and
tools/rename_attack_compare.py (runs the same attacks against A2 and A3 and
records the leak counts as evidence).

Invariant checked: NOTHING that was never beneath the root may come back.
Every secret below lives only outside the root; the attacker moves directories
that WERE inside the root out of it (or swaps what an outside-moved ancestor
contains) at the exact moment the server has opened/listed them.

Only the public tools (read_file / list_dir / grep) are called. The attacker
is synchronised deterministically by wrapping os.scandir / os.pread (the
server calls them through the ``os`` module) and identifying the directory
from /proc/self/fd, so the same hooks work for A2 and A3.
"""
import os
import threading
import time
from unittest import mock

SECRET = "TOPSECRET"


def _fd_path(fd):
    try:
        return os.readlink("/proc/self/fd/%d" % fd)
    except (OSError, TypeError):
        return None


class _HookedIter:
    def __init__(self, real, on_end):
        self.real, self.on_end, self.fired = real, on_end, False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.real.close()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.real)
        except StopIteration:
            if self.on_end and not self.fired:
                self.fired = True
                self.on_end()
            raise

    def close(self):
        self.real.close()


def hook_scandir(target_path, action, when="before"):
    """Run ``action`` once, when the server scandir()s ``target_path``:
    'before' = right after the server opened it, before listing;
    'after'  = after the listing was consumed, before children are opened."""
    real = os.scandir
    state = {"fired": False}

    def wrapper(arg="."):
        if not state["fired"] and isinstance(arg, int) and _fd_path(arg) == target_path:
            state["fired"] = True
            if when == "before":
                action()
                return real(arg)
            return _HookedIter(real(arg), action)
        return real(arg)

    return mock.patch.object(os, "scandir", wrapper), state


def hook_first_pread(action):
    real = os.pread
    state = {"fired": False}

    def wrapper(fd, n, off):
        if not state["fired"]:
            state["fired"] = True
            action()
        return real(fd, n, off)

    return mock.patch.object(os, "pread", wrapper), state


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _call(fn):
    try:
        return fn(), None
    except Exception as e:  # PermissionError / OSError / ... = refused, not leaked
        return None, "%s: %s" % (type(e).__name__, e)


# ------------------------------------------------------------------ deterministic
def attack_grep_subdir_moved(m, root, outside, when="before", no_revalidate=False):
    """grep has opened root/sub; it is moved outside and a secret is created in it."""
    _w(os.path.join(root, "sub", "benign.txt"), "benign\n")
    moved = os.path.join(outside, "moved-sub")

    def act():
        os.rename(os.path.join(root, "sub"), moved)
        _w(os.path.join(moved, "leak.txt"), SECRET + "_GREP_SUB\n")
        os.makedirs(os.path.join(moved, "deeper"))
        _w(os.path.join(moved, "deeper", "leak2.txt"), SECRET + "_GREP_SUB2\n")

    patch, st = hook_scandir(os.path.join(root, "sub"), act, when)
    with patch, _maybe_no_reval(m, no_revalidate):
        out, err = _call(lambda: m.grep(".", SECRET))
    return _result("grep_subdir_moved_" + when + ("_noreval" if no_revalidate else ""), out, err, st)


def attack_grep_ancestor_moved(m, root, outside, when="before", no_revalidate=False):
    """grep is inside root/a/b; the ANCESTOR root/a is moved outside and the
    subtree below it is filled with a secret."""
    _w(os.path.join(root, "a", "b", "c", "f.txt"), "benign\n")
    moved = os.path.join(outside, "moved-a")

    def act():
        os.rename(os.path.join(root, "a"), moved)
        _w(os.path.join(moved, "b", "c", "leak.txt"), SECRET + "_GREP_ANC\n")
        _w(os.path.join(moved, "b", "leak0.txt"), SECRET + "_GREP_ANC0\n")

    patch, st = hook_scandir(os.path.join(root, "a", "b"), act, when)
    with patch, _maybe_no_reval(m, no_revalidate):
        out, err = _call(lambda: m.grep(".", SECRET))
    return _result("grep_ancestor_moved_" + when + ("_noreval" if no_revalidate else ""), out, err, st)


def attack_list_moved(m, root, outside):
    """list_dir has opened root/sub; it is moved outside and gets a secret NAME."""
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    moved = os.path.join(outside, "moved-list")

    def act():
        os.rename(os.path.join(root, "sub"), moved)
        _w(os.path.join(moved, SECRET + "_NAME.txt"), "x\n")

    patch, st = hook_scandir(os.path.join(root, "sub"), act, "before")
    with patch:
        out, err = _call(lambda: m.list_dir("sub"))
    return _result("list_dir_moved", out, err, st)


def attack_read_ancestor_moved(m, root, outside):
    """read_file has opened root/a/b/f.txt; the ancestor root/a is moved outside
    and the file (now outside) is rewritten with a secret before the read."""
    target = os.path.join(root, "a", "b", "f.txt")
    _w(target, "benign\n")
    moved = os.path.join(outside, "moved-read")

    def act():
        os.rename(os.path.join(root, "a"), moved)
        with open(os.path.join(moved, "b", "f.txt"), "r+") as f:
            f.write(SECRET + "_READ\n")

    patch, st = hook_first_pread(act)
    with patch:
        out, err = _call(lambda: m.read_file("a/b/f.txt"))
    return _result("read_ancestor_moved", out, err, st)


# ------------------------------------------------------------------ concurrent
def attack_swap_race(m, root, outside, seconds=3.0):
    """Concurrent attacker: moves root/a OUT, swaps what a/b is while it is
    outside (evil_b, which NEVER enters the root), swaps back and moves a in.
    A resolver that keeps an fd of 'a' across the move and resolves 'b'
    relative to it reads evil_b."""
    _w(os.path.join(root, "a", "b", "f.txt"), "benign\n")
    _w(os.path.join(outside, "evil_b", "f.txt"), SECRET + "_RACE\n")
    _w(os.path.join(outside, "evil_b", SECRET + "_RACE_NAME"), "x\n")
    a, x = os.path.join(root, "a"), os.path.join(outside, "X")
    stop = threading.Event()
    flips = [0]

    def flipper():
        while not stop.is_set():
            try:
                os.rename(a, x)
                os.rename(os.path.join(x, "b"), os.path.join(outside, "b_old"))
                os.rename(os.path.join(outside, "evil_b"), os.path.join(x, "b"))
                os.rename(os.path.join(x, "b"), os.path.join(outside, "evil_b"))
                os.rename(os.path.join(outside, "b_old"), os.path.join(x, "b"))
                os.rename(x, a)
                flips[0] += 1
            except OSError:
                pass

    th = threading.Thread(target=flipper)
    th.start()
    counts = {"calls": 0, "ok": 0, "leaks": 0}
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for fn in (lambda: m.read_file("a/b/f.txt"), lambda: m.list_dir("a/b"),
                       lambda: m.grep("a", SECRET)):
                counts["calls"] += 1
                out, _err = _call(fn)
                if out is not None:
                    counts["ok"] += 1
                    if SECRET in out:
                        counts["leaks"] += 1
    finally:
        stop.set()
        th.join()
    counts["flips"] = flips[0]
    return {"attack": "swap_race", **counts}


class _NoReval:
    def __init__(self, m, on):
        self.m, self.on, self.p = m, on, None

    def __enter__(self):
        if self.on:
            if not hasattr(self.m, "_revalidate"):
                raise RuntimeError("module has no _revalidate")
            self.p = mock.patch.object(self.m, "_revalidate", lambda *a, **k: None)
            self.p.start()

    def __exit__(self, *a):
        if self.p:
            self.p.stop()


def _maybe_no_reval(m, on):
    return _NoReval(m, on)


def _result(name, out, err, st):
    return {"attack": name, "hook_fired": st["fired"], "leaked": bool(out and SECRET in out),
            "output": (out or "")[:400], "error": err}


DETERMINISTIC = [
    lambda m, r, o: attack_grep_subdir_moved(m, r, o, "before"),
    lambda m, r, o: attack_grep_subdir_moved(m, r, o, "after"),
    lambda m, r, o: attack_grep_ancestor_moved(m, r, o, "before"),
    lambda m, r, o: attack_grep_ancestor_moved(m, r, o, "after"),
    attack_list_moved,
    attack_read_ancestor_moved,
]
