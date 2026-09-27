#!/usr/bin/env python3
"""Tests for ro_fs_mcp.py (A3). stdlib unittest; runs with /usr/bin/python3 (3.9+).

Run:  python3 -m unittest -v tests.test_ro_fs_mcp   (from the A3 dir)
"""
import errno
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "ro_fs_mcp.py")
sys.path.insert(0, HERE)
import rename_attacks as RA  # noqa: E402


def load_mod(src=SRC):
    spec = importlib.util.spec_from_file_location("ro_fs_mcp_under_test", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()
        self.tmp = tempfile.mkdtemp(prefix="rofs3-")
        self.base = os.path.realpath(self.tmp)
        # Scratch lives under ~/.kiro (denied): treat it like the workspace carve-out.
        self.m.KIRO_EXCEPTIONS.append(self.base)
        self.root = os.path.join(self.base, "root")
        self.outside = os.path.join(self.base, "outside")
        os.makedirs(os.path.join(self.root, "sub", "deep"))
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "secret.txt"), "w") as f:
            f.write("TOPSECRET\n")
        with open(os.path.join(self.root, "readme.txt"), "w") as f:
            f.write("hello abc\nliteral a.c here\n")
        with open(os.path.join(self.root, "sub", "deep", "x.py"), "w") as f:
            f.write("needle = 1\n")
        self.m.load_roots(self.root)
        self.assertTrue(self.m.ROOTS, self.m.ROOT_ERRORS)

    def tearDown(self):
        for _p, fd in self.m.ROOTS:
            try:
                os.close(fd)
            except OSError:
                pass
        subprocess.run(["chmod", "-R", "u+rwx", self.tmp])
        subprocess.run(["find", self.tmp, "-mindepth", "0", "-delete"])


class TestConfinement(Base):
    def test_openat2_mandatory_no_fallback(self):
        self.assertTrue(self.m.OPENAT2_OK)
        self.assertEqual(self.m.RESOLVE, 1 | 2 | 4 | 8)  # BENEATH|NO_SYMLINKS|NO_MAGICLINKS|NO_XDEV
        for name in ("_walk_open", "USE_OPENAT2", "RO_FS_FORCE_WALK"):
            self.assertNotIn(name, vars(self.m))
        with open(SRC) as f:
            self.assertNotIn("RO_FS_FORCE_WALK", f.read())

    def test_fail_closed_when_openat2_unavailable(self):
        for eno in (errno.ENOSYS, errno.EPERM, errno.EINVAL):
            def boom(*a, _e=eno):
                raise OSError(_e, os.strerror(_e))
            with mock.patch.object(self.m, "_sys_openat2", boom):
                self.m.load_roots(self.root)
            self.assertFalse(self.m.OPENAT2_OK)
            self.assertEqual(self.m.ROOTS, [])
            self.assertIn(self.m.FAIL_CLOSED, self.m.ROOT_ERRORS)
            for call in (lambda: self.m.read_file("readme.txt"), lambda: self.m.list_dir("."),
                         lambda: self.m.grep(".", "hello")):
                with self.assertRaises(PermissionError) as cm:
                    call()
                self.assertIn("fail-closed", str(cm.exception))

    def test_fail_closed_if_beneath_not_enforced(self):
        # A filter that "succeeds" for '..' (BENEATH not honoured) must be refused.
        real = self.m._sys_openat2

        def fake(dirfd, rel, flags, resolve):
            return real(dirfd, "." if rel == ".." else rel, flags, resolve)
        with mock.patch.object(self.m, "_sys_openat2", fake):
            self.m.load_roots(self.root)
        self.assertFalse(self.m.OPENAT2_OK)
        with self.assertRaises(PermissionError):
            self.m.read_file("readme.txt")

    def test_every_access_is_openat2_from_root_with_full_path(self):
        calls = []
        real = self.m._sys_openat2
        rootfd = self.m.ROOTS[0][1]

        def spy(dirfd, rel, flags, resolve):
            calls.append((dirfd, rel, flags, resolve))
            return real(dirfd, rel, flags, resolve)
        os.makedirs(os.path.join(self.root, "sub", "deep", "d3"))
        with open(os.path.join(self.root, "sub", "deep", "d3", "y.txt"), "w") as f:
            f.write("needle 2\n")
        with mock.patch.object(self.m, "_sys_openat2", spy), \
                mock.patch.object(os, "open", side_effect=AssertionError("os.open used")):
            self.m.read_file("sub/deep/x.py")
            self.m.list_dir("sub")
            out = self.m.grep(".", "needle")
        self.assertIn("d3/y.txt", out)
        self.assertTrue(calls)
        for dirfd, rel, _flags, resolve in calls:
            self.assertEqual(dirfd, rootfd, rel)
            self.assertEqual(resolve, self.m.RESOLVE, rel)
        rels = {c[1] for c in calls}
        for want in ("sub/deep/x.py", "sub", ".", "sub/deep", "sub/deep/d3", "sub/deep/d3/y.txt"):
            self.assertIn(want, rels)

    def test_file_opened_once_without_reopen(self):
        calls = []
        real = self.m._sys_openat2

        def spy(dirfd, rel, flags, resolve):
            calls.append((rel, flags))
            return real(dirfd, rel, flags, resolve)
        with mock.patch.object(self.m, "_sys_openat2", spy):
            self.m.read_file("readme.txt")
        # 1st: the one and only data open, O_RDONLY|O_NOFOLLOW|O_NONBLOCK (no O_PATH before it)
        self.assertEqual(calls[0], ("readme.txt", self.m.F_FILE))
        self.assertFalse(calls[0][1] & self.m.O_PATH)
        for fl in (os.O_NOFOLLOW, os.O_NONBLOCK, os.O_NOCTTY):
            self.assertTrue(calls[0][1] & fl)
        # then only the O_PATH revalidation; never a 2nd data open
        self.assertEqual([c for c in calls[1:] if not c[1] & self.m.O_PATH], [])

    def test_fstat_on_the_same_fd_before_reading(self):
        order = []
        real_fstat, real_pread = os.fstat, os.pread
        with mock.patch.object(os, "fstat", lambda fd: (order.append(("fstat", fd)), real_fstat(fd))[1]), \
                mock.patch.object(os, "pread", lambda fd, n, o: (order.append(("pread", fd)), real_pread(fd, n, o))[1]):
            self.m.read_file("readme.txt")
        first_pread = next(i for i, x in enumerate(order) if x[0] == "pread")
        self.assertEqual(order[first_pread - 1], ("fstat", order[first_pread][1]))

    def test_read_ok(self):
        self.assertIn("hello abc", self.m.read_file("readme.txt"))
        self.assertIn("hello abc", self.m.read_file(os.path.join(self.root, "readme.txt")))

    def test_outside_abs_and_dotdot(self):
        for p in (os.path.join(self.outside, "secret.txt"), "/etc/passwd", "sub/../../outside/secret.txt"):
            with self.assertRaises(PermissionError):
                self.m.read_file(p)

    def test_symlinks_never_followed(self):
        os.symlink(os.path.join(self.outside, "secret.txt"), os.path.join(self.root, "ln"))
        os.symlink("readme.txt", os.path.join(self.root, "ln2"))
        os.symlink(self.outside, os.path.join(self.root, "lnd"))
        for p in ("ln", "ln2", "lnd/secret.txt"):
            with self.assertRaises((PermissionError, OSError)):
                self.m.read_file(p)
        with self.assertRaises((PermissionError, OSError)):
            self.m.list_dir("lnd")
        self.assertNotIn("TOPSECRET", self.m.grep(".", "TOPSECRET"))

    def test_special_files_refused_without_hang(self):
        os.mkfifo(os.path.join(self.root, "fifo"))
        s = socket.socket(socket.AF_UNIX)
        s.bind(os.path.join(self.root, "sock"))
        try:
            t0 = time.monotonic()
            for p in ("fifo", "sock", "sub"):
                with self.assertRaises((PermissionError, OSError)):
                    self.m.read_file(p)
            self.assertLess(time.monotonic() - t0, 2)
            self.assertEqual(self.m.grep(".", "zzz_nomatch"), "(no matches)")
        finally:
            s.close()

    def test_no_xdev(self):
        # /proc is another mount: even a root that contained a mount point could
        # not be crossed. Simulate with a root at "/" is refused; check the flag
        # is honoured by the kernel on a real mount boundary under /dev.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(OSError) as cm:
                self.m._sys_openat2(fd, "proc/self", self.m.F_PATH, self.m.RESOLVE)
            self.assertIn(cm.exception.errno, (errno.EXDEV, errno.ELOOP))
            with self.assertRaises(OSError) as cm:
                self.m._sys_openat2(fd, "proc/1", self.m.F_PATH, self.m.RESOLVE)
            self.assertEqual(cm.exception.errno, errno.EXDEV)
        finally:
            os.close(fd)


class TestRenameAttacks(Base):
    """The evaluator's round-2 repro (subdir moved out while grep holds it) and
    ancestor renames during grep/read/list: nothing from outside may leak."""

    def _run(self, attack, **kw):
        res = attack(self.m, self.root, self.outside, **kw)
        self.assertTrue(res["hook_fired"], res)
        self.assertFalse(res["leaked"], res)
        return res

    def test_grep_subdir_moved_before_listing(self):
        self._run(RA.attack_grep_subdir_moved, when="before")

    def test_grep_subdir_moved_after_listing(self):
        self._run(RA.attack_grep_subdir_moved, when="after")

    def test_grep_subdir_moved_after_listing_even_without_revalidation(self):
        # Re-resolution from the root alone (no post-check) already defeats it.
        res = self._run(RA.attack_grep_subdir_moved, when="after", no_revalidate=True)
        self.assertNotIn("leak", res["output"])

    def test_grep_ancestor_moved(self):
        for when in ("before", "after"):
            self.setUp_fresh()
            self._run(RA.attack_grep_ancestor_moved, when=when)

    def test_grep_ancestor_moved_without_revalidation(self):
        self._run(RA.attack_grep_ancestor_moved, when="after", no_revalidate=True)

    def test_list_dir_moved(self):
        res = self._run(RA.attack_list_moved)
        self.assertIn("moved during access", res["error"] or "")

    def test_read_ancestor_moved(self):
        res = self._run(RA.attack_read_ancestor_moved)
        self.assertIn("moved during access", res["error"] or "")

    def test_concurrent_swap_race(self):
        res = RA.attack_swap_race(self.m, self.root, self.outside, seconds=3.0)
        self.assertGreater(res["flips"], 10, res)
        self.assertGreater(res["ok"], 0, res)
        self.assertEqual(res["leaks"], 0, res)

    def setUp_fresh(self):
        self.tearDown()
        self.setUp()


class TestDenyPolicy(Base):
    def test_denied_component_and_names(self):
        os.makedirs(os.path.join(self.root, ".git"))
        for rel in (".git/config", ".env", "k.pem", "id_rsa", "auth.json"):
            with open(os.path.join(self.root, rel), "w") as f:
                f.write("SECRETTOKEN\n")
            with self.assertRaises(PermissionError, msg=rel):
                self.m.read_file(rel)
        listing = self.m.list_dir(".")
        for name in (".git", ".env", "k.pem", "id_rsa", "auth.json"):
            self.assertNotIn(name, listing.split("\n"))
        self.assertIn("denied entries hidden", listing)
        self.assertEqual(self.m.grep(".", "SECRETTOKEN"), "(no matches)")

    def test_roots_refused(self):
        home = self.m.HOME
        bad = os.path.join(self.base, "repo", ".git", "objects")
        os.makedirs(bad)
        for r in (bad, home + "/.ssh", home + "/.aws/x", home + "/.codex", home + "/.claude",
                  home + "/.kiro/crew", home + "/.kiro/settings", "/", "/proc/self"):
            self.m.load_roots(r)
            self.assertEqual(self.m.ROOTS, [], r)

    def test_workspace_exception_lifts_only_the_kiro_rule(self):
        m, H = self.m, self.m.HOME
        ws = H + "/.kiro/crew/workspace"
        self.assertIsNone(m.deny_reason(ws + "/patches/x.md"))
        self.assertIsNotNone(m.deny_reason(H + "/.kiro/crew/config.json"))
        self.assertIsNotNone(m.deny_reason(ws + "/.git/config"))
        self.assertIsNotNone(m.deny_reason(ws + "/p/auth.json"))
        # RO_FS_EXTRA_DENY still applies INSIDE the workspace exception.
        m.EXTRA_DENY[:] = [ws + "/private"]
        self.assertIn("RO_FS_EXTRA_DENY", m.deny_reason(ws + "/private/notes.md"))
        self.assertIsNone(m.deny_reason(ws + "/public/notes.md"))
        m.EXTRA_DENY[:] = [ws]
        self.assertIsNotNone(m.deny_reason(ws + "/patches/x.md"))

    def test_extra_deny_from_env_inside_workspace(self):
        ws = self.m.HOME + "/.kiro/crew/workspace"
        with mock.patch.dict(os.environ, {"RO_FS_EXTRA_DENY": ws + "/secret-proj"}):
            m2 = load_mod()
        self.assertIsNotNone(m2.deny_reason(ws + "/secret-proj/a.txt"))
        self.assertIsNone(m2.deny_reason(ws + "/other/a.txt"))


class TestLimits(Base):
    def test_read_limit_and_offset(self):
        self.m.MAX_READ_BYTES = 4
        out = self.m.read_file("readme.txt")
        self.assertTrue(out.startswith("hell"))
        self.assertIn("offset=4", out)
        for bad in (-1, "3", True):
            with self.assertRaises(ValueError):
                self.m.read_file("readme.txt", offset=bad)

    def test_list_entry_limit_iterative(self):
        for i in range(500):
            open(os.path.join(self.root, "f%03d" % i), "w").close()
        self.m.MAX_ENTRIES = 10
        pulled = [0]
        real = os.scandir

        class Counting:
            def __init__(self, it):
                self.it = it

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.it.close()

            def __iter__(self):
                return self

            def __next__(self):
                pulled[0] += 1
                return next(self.it)
        with mock.patch.object(os, "scandir", lambda a: Counting(real(a))):
            out = self.m.list_dir(".")
        self.assertIn("entry limit", out)
        self.assertLessEqual(len(out.split("\n")), 12)
        self.assertLessEqual(pulled[0], 11)  # never consumed the whole directory

    def test_grep_entry_limit_per_dir(self):
        for i in range(50):
            with open(os.path.join(self.root, "sub", "g%02d.txt" % i), "w") as f:
                f.write("needle\n")
        self.m.MAX_ENTRIES = 10
        out = self.m.grep(".", "needle")
        self.assertIn("entry limit in sub", out)
        self.assertLessEqual(out.count("g"), 50)

    def test_grep_depth_queue_visit_file_limits(self):
        self.m.MAX_DEPTH = 1
        out = self.m.grep(".", "needle")
        self.assertNotIn("x.py", out)
        self.assertIn("depth limit", out)
        self.m.MAX_DEPTH = 12
        self.m.MAX_VISITS = 2
        self.assertIn("visit limit", self.m.grep(".", "needle"))
        self.m.MAX_VISITS = 50000
        self.m.MAX_FILES = 1
        with open(os.path.join(self.root, "z.txt"), "w") as f:
            f.write("q\n")
        self.assertIn("file limit", self.m.grep(".", "needle"))
        self.m.MAX_FILES = 5000
        for i in range(5):
            os.makedirs(os.path.join(self.root, "q%d" % i))
        self.m.MAX_QUEUE = 2
        self.assertIn("queue limit", self.m.grep(".", "needle"))

    def test_grep_byte_limits(self):
        with open(os.path.join(self.root, "big.txt"), "w") as f:
            f.write("needle\n" * 1000)
        self.m.MAX_FILE_BYTES = 100
        out = self.m.grep(".", "needle")
        self.assertNotIn("big.txt", out)
        self.m.MAX_FILE_BYTES = 1 << 20
        self.m.MAX_TOTAL_BYTES = 10
        self.assertIn("byte limit", self.m.grep(".", "needle"))

    def test_fd_limit(self):
        self.m.MAX_OPEN_FDS = 1  # listing needs the dir fd + scandir's dup
        with self.assertRaises(self.m.LimitError):
            self.m.list_dir(".")
        self.assertEqual(self.m._OPEN[0], 0)
        self.m.MAX_OPEN_FDS = 2
        self.assertIn("readme.txt", self.m.list_dir("."))
        self.assertIn("needle", self.m.grep(".", "needle"))  # never more than 2 at once

    def test_no_fd_leak(self):
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(50):
            self.m.read_file("readme.txt")
            self.m.list_dir(".")
            self.m.grep(".", "needle")
            try:
                self.m.read_file("sub")
            except PermissionError:
                pass
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertEqual(self.m._OPEN[0], 0)

    def test_read_file_cooperative_deadline(self):
        with open(os.path.join(self.root, "big.bin"), "wb") as f:
            f.write(b"a" * (1 << 20))
        self.m.MAX_READ_BYTES = 1 << 20
        self.m.TIME_LIMIT = 1.0
        real = os.pread

        def slow(fd, n, o):
            time.sleep(0.3)
            return real(fd, n, o)
        with mock.patch.object(os, "pread", slow):
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                self.m.read_file("big.bin")
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(self.m._OPEN[0], 0)

    def test_read_file_hard_deadline_interrupts_blocked_call(self):
        self.m.TIME_LIMIT = 1.0
        with mock.patch.object(os, "pread", lambda fd, n, o: time.sleep(30)):
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError) as cm:
                self.m.read_file("readme.txt")
        self.assertLess(time.monotonic() - t0, 4)
        self.assertIn("hard time limit", str(cm.exception))

    def test_grep_time_limit_partial(self):
        self.m.TIME_LIMIT = 1.0
        for i in range(20):
            with open(os.path.join(self.root, "t%02d.txt" % i), "w") as f:
                f.write("needle\n")
        real = os.pread

        def slow(fd, n, o):
            time.sleep(0.15)
            return real(fd, n, o)
        with mock.patch.object(os, "pread", slow):
            out = self.m.grep(".", "needle")
        self.assertIn("time limit", out)

    def test_grep_literal_default_and_regex(self):
        out = self.m.grep(".", "a.c")
        self.assertIn("literal a.c here", out)
        self.assertNotIn("hello abc", out)
        self.assertIn("hello abc", self.m.grep(".", "a.c", regex=True))
        with self.assertRaises(ValueError):
            self.m.grep(".", "x" * 1000)
        with self.assertRaises(ValueError):
            self.m.grep(".", r"(a)\1", regex=True)

    def test_grep_redos_killed(self):
        with open(os.path.join(self.root, "evil.txt"), "w") as f:
            f.write("a" * 4000 + "!\n")
        self.m.TIME_LIMIT = 2.0
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.m.grep(".", r"^(a|aa)+$", regex=True)
        self.assertLess(time.monotonic() - t0, 6)

    def test_grep_hit_limit(self):
        with open(os.path.join(self.root, "many.txt"), "w") as f:
            f.write("hit\n" * 500)
        self.m.MAX_HITS = 5
        self.assertIn("hit limit", self.m.grep(".", "hit"))


class TestProtocol(Base):
    def _serve(self, reqs, extra_env=None):
        env = dict(os.environ, RO_FS_ROOTS=self.root, **(extra_env or {}))
        wrapper = ("import runpy,sys;sys.argv=[%r];"
                   "g=runpy.run_path(%r,run_name='lib');g['KIRO_EXCEPTIONS'].append(%r);g['main']()"
                   % (SRC, SRC, self.base))
        p = subprocess.run([sys.executable, "-I", "-c", wrapper],
                           input="\n".join(json.dumps(r) for r in reqs) + "\n",
                           capture_output=True, text=True, env=env, timeout=30)
        return [json.loads(l) for l in p.stdout.splitlines()], p.stderr

    def test_stdio_protocol(self):
        out, _ = self._serve([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {"path": "readme.txt"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "write_file", "arguments": {"path": "x"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {"path": "readme.txt", "mode": "w"}}},
        ])
        self.assertEqual(out[0]["result"]["serverInfo"]["confinement"], "openat2")
        self.assertEqual({t["name"] for t in out[1]["result"]["tools"]}, {"read_file", "list_dir", "grep"})
        for t in out[1]["result"]["tools"]:
            self.assertTrue(t["annotations"]["readOnlyHint"])
            self.assertFalse(t["annotations"]["destructiveHint"])
        self.assertIn("hello abc", out[2]["result"]["content"][0]["text"])
        self.assertTrue(out[3]["result"]["isError"])
        self.assertTrue(out[4]["result"]["isError"])

    def test_server_fail_closed_when_openat2_blocked(self):
        # Simulate a seccomp'd kernel: syscall number replaced by an invalid one.
        wrapper = ("import runpy,sys;sys.argv=[%r];g=runpy.run_path(%r,run_name='lib');"
                   "g['_sys_openat2'].__globals__['_SYS_OPENAT2']=99999;g['main']()" % (SRC, SRC))
        env = dict(os.environ, RO_FS_ROOTS=self.root)
        p = subprocess.run([sys.executable, "-I", "-c", wrapper], input=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {"path": "readme.txt"}}}) + "\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}) + "\n",
            capture_output=True, text=True, env=env, timeout=30)
        out = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertTrue(out[0]["result"]["isError"])
        self.assertIn("fail-closed", out[0]["result"]["content"][0]["text"])
        self.assertEqual(out[1]["result"]["serverInfo"]["confinement"], "fail-closed")
        self.assertIn("fail-closed", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
