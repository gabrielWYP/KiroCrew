#!/usr/bin/env python3
"""Read-only filesystem MCP server (stdio) for codex-evaluator -- A3.

Tools: read_file, list_dir, grep. No write/exec/network code path exists.

Confinement (A3 closes the rename TOCTOU that A2 still had in grep):
  * openat2(2) is MANDATORY. It is probed at startup; if the kernel or a
    seccomp filter refuses it, NO root is opened and every tool call fails
    ("openat2 unavailable: fail-closed"). There is no openat() walk fallback.
  * Every root is opened ONCE at startup (openat2 from "/", no symlinks) and
    kept as a directory fd. It is the only long-lived fd.
  * EVERY object touched -- the read_file target, the list_dir directory, and
    each directory AND each file visited by grep -- is resolved with ONE call
      openat2(root_fd, "<full relative path from the root>",
              RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS|RESOLVE_NO_XDEV)
    No sub-directory fd is ever kept and nothing is opened relative to one:
    grep queues relative PATHS, not fds. A directory renamed out of the root
    after it was listed is simply not found when its children are opened
    (ENOENT), and a rename during resolution is caught by the kernel's final
    path_is_under() check for scoped lookups (EXDEV).
  * Files are opened ONCE, directly with O_RDONLY|O_NOFOLLOW|O_NONBLOCK|
    O_NOCTTY, and fstat() on THAT fd must say S_ISREG before a single byte is
    read (directories: S_ISDIR before listing). There is no O_PATH->reopen.
  * After reading/listing, the same relative path is resolved again from the
    root (O_PATH) and must still be the same (st_dev, st_ino); otherwise the
    result is discarded ("object moved during access").

Deny policy (evaluated on the ABSOLUTE path, root prefix included):
  * DENY_PREFIXES: ~/.ssh ~/.aws ~/.gnupg ~/.codex* ~/.claude* ~/.kiro
    ~/.config/{gh,gcloud,gogcli} ~/.docker ~/.kube ~/.netrc ... /proc /sys /dev.
    The ONLY exception is ~/.kiro/crew/workspace, and it exempts the ~/.kiro
    rule only: RO_FS_EXTRA_DENY and every other prefix still apply inside it.
  * DENY_COMPONENTS anywhere in the path, DENY_NAME_GLOBS on the leaf.
  * A ROOT that is "/", or falls inside a denied prefix, or has a denied
    component, is refused at startup.

Limits (env-overridable, all enforced per call): bytes per read, entries per
directory (scandir is consumed iteratively and stopped at the cap), grep
depth / queued dirs / visited entries / files / file size / total bytes /
hits, simultaneously open fds, and a wall-clock deadline for EVERY tool
(cooperative checks plus an ITIMER_REAL hard stop that interrupts a blocked
syscall). grep is LITERAL by default; regex mode has a pattern-length cap,
refuses back-references and runs in a forked worker killed at the deadline.

Python >= 3.9, stdlib only, Linux >= 5.6 only.
"""
import ctypes
import errno
import fnmatch
import functools
import json
import os
import re
import select
import signal
import stat
import sys
import threading
import time


# ---------------------------------------------------------------- limits
def _env_int(name, default):
    try:
        v = int(os.environ.get(name, default))
        return v if v > 0 else default
    except ValueError:
        return default


MAX_READ_BYTES = _env_int("RO_FS_MAX_BYTES", 262144)
MAX_OFFSET = _env_int("RO_FS_MAX_OFFSET", 1 << 34)
MAX_ENTRIES = _env_int("RO_FS_MAX_ENTRIES", 2000)        # per directory (list_dir and grep)
MAX_DEPTH = _env_int("RO_FS_MAX_DEPTH", 12)
MAX_QUEUE = _env_int("RO_FS_MAX_QUEUE", 2000)            # grep: dirs waiting to be visited
MAX_VISITS = _env_int("RO_FS_MAX_VISITS", 50000)         # grep: entries examined in total
MAX_FILES = _env_int("RO_FS_MAX_FILES", 5000)
MAX_FILE_BYTES = _env_int("RO_FS_MAX_FILE_BYTES", 2 * 1024 * 1024)
MAX_TOTAL_BYTES = _env_int("RO_FS_MAX_TOTAL_BYTES", 64 * 1024 * 1024)
MAX_HITS = _env_int("RO_FS_MAX_HITS", 200)
MAX_PATTERN = _env_int("RO_FS_MAX_PATTERN", 256)
MAX_OPEN_FDS = _env_int("RO_FS_MAX_OPEN_FDS", 8)         # besides the root fds
MAX_PATH_LEN = 4096
MAX_LINE = 4096
CHUNK = 65536
TIME_LIMIT = float(_env_int("RO_FS_TIME_LIMIT", 10))
HARD_GRACE = 1.0  # ITIMER_REAL fires this long after the cooperative deadline

# ---------------------------------------------------------------- deny policy
HOME = os.path.realpath(os.path.expanduser("~"))


def _h(p):
    return os.path.join(HOME, p)


KIRO_PREFIX = _h(".kiro")
# The single carve-out: it lifts ONLY the KIRO_PREFIX rule (a list so tests
# can add their scratch dir, which lives under ~/.kiro; production has one).
KIRO_EXCEPTIONS = [_h(".kiro/crew/workspace")]
DENY_PREFIXES = [
    _h(".ssh"), _h(".aws"), _h(".gnupg"), _h(".codex"), _h(".codex-evaluator"),
    _h(".claude"), _h(".claude.json"), KIRO_PREFIX, _h(".config/gh"),
    _h(".config/gcloud"), _h(".config/gogcli"), _h(".docker"), _h(".kube"),
    _h(".netrc"), _h(".git-credentials"), _h(".pki"), _h(".password-store"),
    "/root", "/etc/shadow", "/etc/gshadow", "/etc/ssh", "/proc", "/sys", "/dev",
    "/run", "/var/run",
]
EXTRA_DENY = [os.path.realpath(p) for p in
              os.environ.get("RO_FS_EXTRA_DENY", "").split(os.pathsep) if p]
DENY_COMPONENTS = {".git", ".ssh", ".aws", ".gnupg", ".codex", ".codex-evaluator",
                   ".claude", ".docker", ".kube", ".password-store", "node_modules",
                   "crew-auth-staging"}
DENY_NAME_GLOBS = ["*.pem", "*.key", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
                   ".env", ".env.*", "auth.json", "credentials", "credentials.*",
                   ".netrc", ".git-credentials", "*.p12", "*.pfx", "*.kdbx",
                   "*.keystore", "*.jks", "session_token_*", "telemetry_salt",
                   ".pgpass", ".npmrc", ".pypirc"]


def _under(path, prefix):
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def deny_reason(abspath):
    """Why ``abspath`` (normalized, absolute, symlink-free) is denied, or None."""
    for p in EXTRA_DENY:
        if _under(abspath, p):
            return "denied prefix %s (RO_FS_EXTRA_DENY)" % p
    for p in DENY_PREFIXES:
        if _under(abspath, p):
            if p == KIRO_PREFIX and any(_under(abspath, e) for e in KIRO_EXCEPTIONS):
                continue  # the workspace carve-out lifts the ~/.kiro rule only
            return "denied prefix %s" % p
    parts = [c for c in abspath.split("/") if c]
    for c in parts:
        if c in DENY_COMPONENTS:
            return "denied component %s" % c
    if parts:
        leaf = parts[-1]
        for g in DENY_NAME_GLOBS:
            if fnmatch.fnmatchcase(leaf, g):
                return "denied name %s" % leaf
    return None


# ---------------------------------------------------------------- openat2
O_PATH = getattr(os, "O_PATH", 0o10000000)
RESOLVE_NO_XDEV, RESOLVE_NO_MAGICLINKS, RESOLVE_NO_SYMLINKS, RESOLVE_BENEATH = 1, 2, 4, 8
# Fixed, not configurable: every access below a root.
RESOLVE = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV
_SYS_OPENAT2 = 437  # same number on x86_64 and aarch64 (asm-generic)

F_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
F_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
F_PATH = O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC  # openat2 admits only these with O_PATH


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                ("resolve", ctypes.c_uint64)]


try:
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.syscall.restype = ctypes.c_long
except OSError:
    _libc = None


def _sys_openat2(dirfd, rel, flags, resolve):
    if _libc is None:
        raise OSError(38, "openat2 unavailable (no libc)", rel)
    how = _OpenHow(flags, 0, resolve)
    fd = _libc.syscall(_SYS_OPENAT2, ctypes.c_int(dirfd), rel.encode("utf-8", "surrogateescape"),
                       ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how)))
    if fd < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), rel)
    return fd


OPENAT2_OK = False
FAIL_CLOSED = "openat2 unavailable: ro_fs refuses every access (fail-closed)"


def _probe_openat2():
    """True only if openat2 works with the exact RESOLVE flags used later."""
    global OPENAT2_OK
    OPENAT2_OK = False
    try:
        slash = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        os.close(_sys_openat2(slash, ".", F_PATH | os.O_DIRECTORY, RESOLVE))
        # ".." from the scope root must be refused with EXDEV: proves the
        # kernel really applies RESOLVE_BENEATH (not a filter faking success).
        try:
            os.close(_sys_openat2(slash, "..", F_PATH | os.O_DIRECTORY, RESOLVE))
            return False
        except OSError as e:
            if e.errno != errno.EXDEV:
                return False
        OPENAT2_OK = True
    except OSError:
        OPENAT2_OK = False
    finally:
        os.close(slash)
    return OPENAT2_OK


# ---------------------------------------------------------------- fd budget / deadline
_OPEN = [0]
_DEADLINE = [None]


class LimitError(Exception):
    pass


def _open(rfd, rel, flags):
    """The ONLY way anything below a root is opened."""
    if not OPENAT2_OK:
        raise PermissionError(FAIL_CLOSED)
    if _OPEN[0] >= MAX_OPEN_FDS:
        raise LimitError("open-descriptor limit (%d) reached" % MAX_OPEN_FDS)
    fd = _sys_openat2(rfd, rel or ".", flags, RESOLVE)
    _OPEN[0] += 1
    return fd


def _close(fd):
    _OPEN[0] -= 1
    os.close(fd)


def _check_deadline():
    d = _DEADLINE[0]
    if d is not None and time.monotonic() > d:
        raise TimeoutError("time limit (%.0fs) reached" % TIME_LIMIT)


def _on_alarm(_sig, _frm):
    raise TimeoutError("hard time limit (%.0fs) reached" % (TIME_LIMIT + HARD_GRACE))


class _CallGuard:
    """Per tool call: cooperative deadline + ITIMER_REAL hard stop + fd accounting."""

    def __enter__(self):
        _DEADLINE[0] = time.monotonic() + TIME_LIMIT
        self._timer = threading.current_thread() is threading.main_thread()
        if self._timer:
            self._old = signal.signal(signal.SIGALRM, _on_alarm)
            signal.setitimer(signal.ITIMER_REAL, TIME_LIMIT + HARD_GRACE)
        return self

    def __exit__(self, *exc):
        if self._timer:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._old)
        _DEADLINE[0] = None
        if _OPEN[0] != 0:  # a leak would be a bug; never let it accumulate silently
            sys.stderr.write("ro_fs: fd accounting drift %d\n" % _OPEN[0])
            _OPEN[0] = 0
        return False


def _guarded(fn):
    """Public tools always run under a _CallGuard (nested calls reuse it)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if _DEADLINE[0] is not None:
            return fn(*a, **kw)
        with _CallGuard():
            return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- roots
ROOTS = []  # list of (abs_path, fd)
ROOT_ERRORS = []


def _open_root(real):
    slash = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        # From "/", no symlinks, no magic links. NO_XDEV is NOT applied here
        # (a root may legitimately sit on another mount); it IS applied to
        # everything opened beneath the root.
        return _sys_openat2(slash, real.lstrip("/") or ".", F_DIR,
                            RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)
    finally:
        os.close(slash)


def load_roots(spec):
    for _r, fd in ROOTS:
        try:
            os.close(fd)
        except OSError:
            pass
    del ROOTS[:]
    del ROOT_ERRORS[:]
    if not _probe_openat2():
        ROOT_ERRORS.append(FAIL_CLOSED)
    else:
        for r in [r for r in spec.split(os.pathsep) if r]:
            if not os.path.isabs(r):
                ROOT_ERRORS.append("root not absolute: %s" % r)
                continue
            real = os.path.realpath(r)
            if real == "/":
                ROOT_ERRORS.append("root '/' refused")
                continue
            why = deny_reason(real)
            if why:
                ROOT_ERRORS.append("root %s refused: %s" % (real, why))
                continue
            try:
                fd = _open_root(real)
            except OSError as e:
                ROOT_ERRORS.append("root %s unusable: %s" % (real, e))
                continue
            ROOTS.append((real, fd))
    for m in ROOT_ERRORS:
        sys.stderr.write("ro_fs: %s\n" % m)


def _split(path):
    """-> (root_abs, root_fd, rel, abs_path) or raise PermissionError."""
    if not OPENAT2_OK:
        raise PermissionError(FAIL_CLOSED)
    if not ROOTS:
        raise PermissionError("no usable roots (RO_FS_ROOTS empty or all refused)")
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > MAX_PATH_LEN:
        raise PermissionError("invalid path")
    if os.path.isabs(path):
        cands = [(r, fd) for r, fd in ROOTS if _under(path.rstrip("/") or "/", r)]
        if not cands:
            raise PermissionError("outside allowed roots: %s" % path)
        root, rfd = max(cands, key=lambda x: len(x[0]))
        rel = path[len(root):]
    else:
        root, rfd = ROOTS[0]
        rel = path
    parts = [c for c in rel.split("/") if c and c != "."]
    if ".." in parts:
        raise PermissionError("'..' not allowed: %s" % path)
    absp = "/".join([root.rstrip("/")] + parts) if parts else root
    why = deny_reason(absp)
    if why:
        raise PermissionError("%s: %s" % (why, path))
    return root, rfd, "/".join(parts), absp


# ---------------------------------------------------------------- confined primitives
def _revalidate(rfd, rel, st):
    """The path must still resolve (from the root) to the object we used."""
    try:
        fd = _open(rfd, rel, F_PATH | (os.O_DIRECTORY if stat.S_ISDIR(st.st_mode) else 0))
    except TimeoutError:
        raise
    except OSError:
        raise PermissionError("object moved during access: %s" % (rel or "."))
    try:
        st2 = os.fstat(fd)
    finally:
        _close(fd)
    if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
        raise PermissionError("object replaced during access: %s" % (rel or "."))


def _read_regular(rfd, rel, offset, limit, max_size=None):
    """Open ONCE (O_RDONLY|O_NOFOLLOW|O_NONBLOCK via openat2), fstat THAT fd,
    read at most ``limit`` bytes from ``offset`` in deadline-checked chunks.
    -> (bytes, stat). ``max_size``: refuse larger files without reading."""
    fd = _open(rfd, rel, F_FILE)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError("not a regular file: %s" % rel)
        if max_size is not None and st.st_size > max_size:
            raise LimitError("file larger than %d bytes" % max_size)
        buf, pos = [], offset
        left = limit
        while left > 0:
            _check_deadline()
            b = os.pread(fd, min(CHUNK, left), pos)
            if not b:
                break
            buf.append(b)
            pos += len(b)
            left -= len(b)
    finally:
        _close(fd)
    _revalidate(rfd, rel, st)
    return b"".join(buf), st


def _scan_dir(rfd, rel, cap):
    """Open the directory via openat2, fstat THAT fd, consume scandir
    iteratively up to ``cap`` entries. -> ([(name, kind)], truncated)."""
    fd = _open(rfd, rel, F_DIR)
    out, truncated = [], False
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise PermissionError("not a directory: %s" % rel)
        if _OPEN[0] >= MAX_OPEN_FDS:  # os.scandir(fd) dups the fd
            raise LimitError("open-descriptor limit (%d) reached" % MAX_OPEN_FDS)
        _OPEN[0] += 1
        try:
            with os.scandir(fd) as it:
                for e in it:
                    _check_deadline()
                    if len(out) >= cap:
                        truncated = True
                        break
                    out.append((e.name, _entry_kind(e)))
        finally:
            _OPEN[0] -= 1
    finally:
        _close(fd)
    _revalidate(rfd, rel, st)
    return out, truncated


def _entry_kind(e):
    """d_type hint only (never followed); what is opened is re-checked by fstat."""
    try:
        if e.is_symlink():
            return "@"
        if e.is_dir(follow_symlinks=False):
            return "/"
        if e.is_file(follow_symlinks=False):
            return ""
    except OSError:
        pass
    return "?"  # fifo/socket/device/unknown: listed, never opened


def _join(rel, name):
    return name if not rel else rel + "/" + name


# ---------------------------------------------------------------- tools
@_guarded
def read_file(path, offset=0):
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > MAX_OFFSET:
        raise ValueError("offset must be an integer in [0, %d]" % MAX_OFFSET)
    _root, rfd, rel, _abs = _split(path)
    data, _st = _read_regular(rfd, rel, offset, MAX_READ_BYTES + 1)
    more = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", "replace")
    return text + ("\n[truncated; continue with offset=%d]" % (offset + MAX_READ_BYTES) if more else "")


@_guarded
def list_dir(path):
    _root, rfd, rel, absp = _split(path)
    entries, truncated = _scan_dir(rfd, rel, MAX_ENTRIES)
    out, hidden = [], 0
    for name, kind in entries:  # already capped: sorting is bounded
        if deny_reason(absp.rstrip("/") + "/" + name):
            hidden += 1
            continue
        out.append(name + kind)
    out.sort()
    if truncated:
        out.append("[entry limit %d reached]" % MAX_ENTRIES)
    if hidden:
        out.append("[%d denied entries hidden]" % hidden)
    return "\n".join(out) or "(empty)"


def _compile(pattern, regex, ignore_case):
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN:
        raise ValueError("pattern longer than %d" % MAX_PATTERN)
    if not regex:
        needle = pattern.lower() if ignore_case else pattern
        return lambda line: needle in (line.lower() if ignore_case else line)
    if re.search(r"\\[1-9]|\(\?P=", pattern):
        raise ValueError("back-references are not allowed")
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    return lambda line: rx.search(line) is not None


def _grep_walk(rfd, rel0, abs0, match):
    """Iterative walk over relative PATHS. Every directory and every file is
    resolved from the root fd with a single openat2; no sub-directory fd
    outlives the scan of that directory."""
    hits, notes = [], []
    files = nbytes = visits = 0
    stack = [(rel0, abs0, 0)]
    try:
        while stack:
            rel, absd, depth = stack.pop()
            try:
                entries, truncated = _scan_dir(rfd, rel, MAX_ENTRIES)
            except TimeoutError:
                raise
            except (OSError, PermissionError, LimitError) as e:
                if rel == rel0:
                    raise
                notes.append("skipped %s: %s" % (rel, e.__class__.__name__))
                continue
            if truncated:
                notes.append("entry limit in %s" % (rel or "."))
            entries.sort()
            subdirs = []
            for name, kind in entries:
                visits += 1
                if visits > MAX_VISITS:
                    return hits, "visit limit", notes
                crel, cabs = _join(rel, name), absd.rstrip("/") + "/" + name
                if deny_reason(cabs):
                    continue
                if kind == "/":
                    if depth + 1 > MAX_DEPTH:
                        if "depth limit" not in notes:
                            notes.append("depth limit")
                        continue
                    subdirs.append((crel, cabs, depth + 1))
                    continue
                if kind != "":
                    continue  # symlinks, fifos, sockets, devices: never opened
                if files >= MAX_FILES:
                    return hits, "file limit", notes
                try:
                    data, st = _read_regular(rfd, crel, 0, MAX_FILE_BYTES, max_size=MAX_FILE_BYTES)
                except TimeoutError:
                    raise
                except (OSError, PermissionError, LimitError):
                    continue
                files += 1
                nbytes += len(data)
                if nbytes > MAX_TOTAL_BYTES:
                    return hits, "byte limit", notes
                if b"\x00" in data[:8192]:
                    continue  # binary
                for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if match(line[:MAX_LINE]):
                        hits.append("%s:%d:%s" % (cabs, n, line.rstrip()[:300]))
                        if len(hits) >= MAX_HITS:
                            return hits, "hit limit", notes
            for sd in reversed(subdirs):
                if len(stack) >= MAX_QUEUE:
                    return hits, "queue limit", notes
                stack.append(sd)
    except TimeoutError:
        return hits, "time limit", notes
    return hits, None, notes


def _format(hits, limit_msg, notes):
    out = "\n".join(hits) or "(no matches)"
    if limit_msg:
        out += "\n[%s reached]" % limit_msg
    if notes:
        out += "\n[%s]" % "; ".join(notes[:20])
    return out


@_guarded
def grep(path, pattern, regex=False, ignore_case=False):
    if not isinstance(regex, bool) or not isinstance(ignore_case, bool):
        raise ValueError("regex/ignore_case must be booleans")
    match = _compile(pattern, regex, ignore_case)
    _root, rfd, rel, absp = _split(path)
    if not regex:
        return _format(*_grep_walk(rfd, rel, absp, match))
    # Regex: forked worker, SIGKILLed at the deadline (ReDoS bound).
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: ITIMER_REAL is not inherited; set its own alarm
        try:
            os.close(r)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(int(TIME_LIMIT) + 2)
            res = _grep_walk(rfd, rel, absp, match)
            os.write(w, json.dumps(res).encode())
        finally:
            os._exit(0)
    os.close(w)
    buf = b""
    try:
        while True:
            left = (_DEADLINE[0] or time.monotonic() + TIME_LIMIT) + 0.5 - time.monotonic()
            if left <= 0:
                raise TimeoutError("regex grep exceeded %.0fs; use a literal pattern" % TIME_LIMIT)
            rd, _, _ = select.select([r], [], [], left)
            if not rd:
                continue
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
    finally:
        os.close(r)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
    if not buf:
        raise TimeoutError("regex grep worker died (time limit?); use a literal pattern")
    return _format(*json.loads(buf.decode()))


def call_tool(name, args):
    """Run one tool under the per-call deadline / fd budget."""
    fn = HANDLERS.get(name)
    if fn is None:
        raise ValueError("unknown tool")
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    return fn(**args)  # each tool is @_guarded


# ---------------------------------------------------------------- MCP stdio
_RO = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
TOOLS = [
    {"name": "read_file", "description": "Read a UTF-8 text file (read-only, regular files only, max %d bytes per call)." % MAX_READ_BYTES,
     "annotations": _RO,
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
         "required": ["path"]}},
    {"name": "list_dir", "description": "List a directory (read-only, max %d entries). Suffix: / dir, @ symlink (not followed), ? special." % MAX_ENTRIES,
     "annotations": _RO,
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "grep", "description": "Search text under a directory (read-only). LITERAL by default; regex=true for a bounded regex.",
     "annotations": _RO,
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "path": {"type": "string"}, "pattern": {"type": "string", "maxLength": MAX_PATTERN},
         "regex": {"type": "boolean"}, "ignore_case": {"type": "boolean"}},
         "required": ["path", "pattern"]}},
]
HANDLERS = {"read_file": read_file, "list_dir": list_dir, "grep": grep}


def reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if rid is None:
        return  # notification
    if method == "initialize":
        reply(rid, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ro-fs", "version": "3.0",
                                   "confinement": "openat2" if OPENAT2_OK else "fail-closed"}})
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        try:
            text, err = call_tool(params.get("name"), params.get("arguments") or {}), False
        except Exception as exc:  # report, never crash
            text, err = "%s: %s" % (type(exc).__name__, exc), True
        reply(rid, {"content": [{"type": "text", "text": text}], "isError": err})
    elif method == "ping":
        reply(rid, {})
    else:
        reply(rid, error={"code": -32601, "message": "method not found: %s" % method})


def main():
    load_roots(os.environ.get("RO_FS_ROOTS", ""))
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if isinstance(req, dict):
            handle(req)


if __name__ == "__main__":
    main()
