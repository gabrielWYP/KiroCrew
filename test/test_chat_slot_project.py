"""Tests for POST /api/chat/slots/{slot}/project endpoint."""

from __future__ import annotations

import errno
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_project
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.sandbox import IDENTITY_UNAVAILABLE


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.file_indexes = MagicMock()
    state.file_indexes.acquire = AsyncMock()
    state.file_indexes.release = AsyncMock()
    return state


class TestChatSlotProject:
    @pytest.mark.asyncio
    async def test_set_project(self, tmp_path):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert data["project"] == str(tmp_path)
                assert slot.project == str(tmp_path)

    @pytest.mark.asyncio
    async def test_clear_project(self, tmp_path):
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": ""},
            )
            assert resp.status == 200
            assert slot.project == ""

    @pytest.mark.asyncio
    async def test_nonexistent_dir_returns_400(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": "/nonexistent_xyz_123"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_sensitive_path_returns_403(self, tmp_path):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_folders.is_sensitive_resolved_path", return_value=True
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unc", [r"\\evil\share\proj", "//evil/share/proj", r"\\?\UNC\evil\share\proj"]
    )
    async def test_a_unc_project_is_refused_before_realpath(self, monkeypatch, unc):
        """The project endpoint is the HTTP twin of the ``set_project`` directive:
        request-body path text reaching ``realpath``, which on a Windows gateway
        opens an SMB connection to a UNC host. It runs the folder endpoint's own
        lexical UNC refusal first (one helper across the admission sites), in
        the folder validator's 400 shape, audited; nothing is probed or set."""
        monkeypatch.setattr("kiro_crew.dashboard.chat_folders.unc_probe_allowed", lambda raw: False)
        touched = MagicMock(side_effect=AssertionError("filesystem touched for a UNC project"))
        monkeypatch.setattr("os.path.realpath", touched)
        monkeypatch.setattr("os.path.isdir", touched)
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers.sel") as sel_fn:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/project", json={"project": unc})
                body = await resp.json()
        assert resp.status == 400, body
        assert body == {
            "error": "Project directory must not be a network (UNC) path",
            "code": "project_unc_path",
        }
        touched.assert_not_called()
        assert slot.project == ""
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "chat_slot_project"
        assert kwargs["outcome"] == "denied"
        assert "UNC" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_data_home_overlap_returns_actionable_400(self, tmp_path, monkeypatch):
        """Pre-flight: a workspace containing the voice runtime is refused
        at the endpoint with the actionable message, before any session spawn."""
        import kiro_crew.sandbox as sandbox_mod

        # The pre-flight is darwin-gated to match the spawn-time guards it
        # mirrors, so pin the platform for the refusal path.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": str(tmp_path)},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "workspace_overlaps_data_home"
            assert "protected voice runtime" in data["error"]
            # The guard message embeds paths with !r, so on Windows the
            # backslashes are repr-escaped — assert the repr form, which is the
            # exact token the formatter emits on every platform.
            assert repr(str(runtime)) in data["error"]
            assert "Pick a project subdirectory" in data["error"]
            assert slot.project != str(tmp_path)

    @pytest.mark.asyncio
    async def test_can_change_mid_session(self, tmp_path):
        """Unlike workspace, project can be changed after messages are sent."""
        slot = _ChatSlot("test")
        slot.total_messages = 5
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
                assert slot.project == str(tmp_path)

    @pytest.mark.asyncio
    async def test_slot_not_found(self):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/missing/project",
                json={"project": "/tmp"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_change_defers_session_reset(self, tmp_path):
        """Endpoint sets the deferred-reset flag instead of resetting inline,
        because an inline reset would killpg the MCP-core child that called it.
        chat_runner consumes the flag so the next message picks up the new CWD."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        # Reset is deferred — endpoint must NOT call it inline.
        state.sessions.reset.assert_not_awaited()
        # Flag is set on the slot so chat_runner can consume it at the turn boundary.
        assert slot._pending_reset_history_key == "dashboard:test"

    @pytest.mark.asyncio
    async def test_unchanged_does_not_set_pending_reset(self, tmp_path):
        """No-op when project doesn't change: no inline reset and no flag set."""
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key is None


class TestFolderProjectDirOverlapPreflight:
    """The folder ``project_dir`` write path is the third
    user-driven project chokepoint — it must refuse a data-home overlap at the
    moment of choice with the SAME message as the endpoint and set_project.
    The check lives in ``_folder_project_overlap_denied`` (run off-loop
    by the create/update handlers), NOT in ``_validate_project_dir``, which the
    slot-create read path re-runs against stored values."""

    def _pin_runtime(self, tmp_path, monkeypatch):
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        return runtime

    def test_folder_overlap_denied_with_guard_message(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_folders import _folder_project_overlap_denied

        runtime = self._pin_runtime(tmp_path, monkeypatch)
        err = _folder_project_overlap_denied(str(tmp_path))
        assert err is not None
        # Byte-identical family: same formatter as endpoint + spawn guard.
        assert "protected voice runtime" in err
        assert repr(str(runtime)) in err
        assert "Pick a project subdirectory" in err

    def test_folder_overlap_check_accepts_non_overlapping_dir(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_folders import _folder_project_overlap_denied

        self._pin_runtime(tmp_path, monkeypatch)
        clean = tmp_path / "clean"
        clean.mkdir()
        assert _folder_project_overlap_denied(str(clean)) is None


class TestTheProjectIdentityLivesForTheGatewayProcess:
    """``project_identity`` is recorded IN MEMORY for the gateway process's
    lifetime and never persisted: ``st_dev`` is reassigned across mounts on
    anonymous-device filesystems (a stored pair would refuse every spawn of an
    unchanged directory after a reboot) and the transcript store is
    agent-writable (a stored pair could be forged to pass the swap check). A
    project with no record in this process -- every slot after a restart -- is
    re-pinned at its first spawn by the same fenced pin the bind uses
    (``spawn_project_identity_repinned``); the record applies only while its
    spelling is the slot's current project (``spawn_project_identity``)."""

    def test_the_record_applies_to_its_spelling_and_nothing_else(self):
        from kiro_crew.dashboard.state import (
            _ChatSlot,
            record_project_identity,
            spawn_project_identity,
        )

        slot = _ChatSlot("dashboard:src")
        slot.project = "/srv/proj"
        record_project_identity(slot, (64769, 1310725))
        assert slot.project_identity == ("/srv/proj", 64769, 1310725)
        assert spawn_project_identity(slot) == (64769, 1310725)
        slot.project = "/srv/other"  # re-pointed without a record: no record in this process
        assert spawn_project_identity(slot) is None
        record_project_identity(slot, (5, 6))
        assert slot.project_identity == ("/srv/other", 5, 6)
        record_project_identity(slot, None)
        assert slot.project_identity is None

    @pytest.mark.asyncio
    async def test_a_restart_re_pins_at_the_first_spawn_and_a_later_swap_is_refused(self, tmp_path):
        """After a restart the slot carries no record: the first spawn re-pins
        the bound path as it stands and passes on an unchanged directory; the
        record then holds for the process, so a swap after it is refused at the
        next spawn. A directory the re-pin cannot open (a link planted at the
        name across the restart) REFUSES the spawn; only a slot with no binding
        is left unexamined."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import (
            _ChatSlot,
            spawn_project_identity,
            spawn_project_identity_repinned,
        )

        proj = tmp_path / "proj"
        proj.mkdir()
        info = os.stat(proj)
        slot = _ChatSlot("dashboard:restarted")
        slot.project = str(proj)  # restored from the transcript: the path only
        assert spawn_project_identity(slot) is None
        pinned = await spawn_project_identity_repinned(slot)
        assert pinned == (info.st_dev, info.st_ino)
        assert slot.project_identity == (str(proj), info.st_dev, info.st_ino)
        real, fd = sandbox.verify_agent_workspace_for_spawn(str(proj), pinned)
        sandbox.release_agent_workspace_fd(fd)
        assert os.path.realpath(real) == os.path.realpath(proj)
        # The swap, after the in-process pin: refused at the next spawn.
        other = tmp_path / ".proj.other"
        other.mkdir()
        proj.rmdir()
        other.rename(proj)
        assert await spawn_project_identity_repinned(slot) == pinned  # the record holds
        with pytest.raises(sandbox.AgentWorkspacePinRefused, match="different directory"):
            sandbox.verify_agent_workspace_for_spawn(str(proj), pinned)
        # A LINK planted at the bound name across the restart: the re-pin
        # REFUSES (the governed reason and the remedy), never answers None --
        # a None would skip the check for every slot after a restart.
        linked = _ChatSlot("dashboard:linked")
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
            linked.project = str(link)
            expected_reason = "link or not a directory"
        except (OSError, NotImplementedError):
            linked.project = str(tmp_path / "gone")  # no link granted: a missing leaf
            expected_reason = "missing"
        with pytest.raises(sandbox.WorkspacePinFailed, match="re-bind the project directory") as ei:
            await spawn_project_identity_repinned(linked)
        assert expected_reason in str(ei.value) and "could not be re-pinned" in str(ei.value)
        assert linked.project_identity is None
        # A slot with no binding at all is the ONE shape that is not examined.
        unbound = _ChatSlot("dashboard:unbound")
        unbound.project = ""
        assert await spawn_project_identity_repinned(unbound) is None

    @pytest.mark.asyncio
    async def test_a_project_re_pointed_while_the_re_pin_ran_pins_the_new_spelling(
        self, tmp_path, monkeypatch
    ):
        """The re-pin runs off the loop; a directive that re-points ``project``
        in between must not leave a record naming the OLD spelling for the new
        directory: the new spelling is pinned instead."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard import state as state_mod
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        first = tmp_path / "first"
        first.mkdir()
        second = tmp_path / "second"
        second.mkdir()
        slot = _ChatSlot("dashboard:repointed")
        slot.project = str(first)
        real_pin = sandbox.directory_identity_pinned
        calls: list[str] = []

        def _pin_then_repoint(path):
            calls.append(str(path))
            identity = real_pin(path)
            if len(calls) == 1:
                slot.project = str(second)  # the re-point, mid-pin
            return identity

        monkeypatch.setattr(sandbox, "directory_identity_pinned", _pin_then_repoint)
        assert state_mod is not None
        info = os.stat(second)
        assert await spawn_project_identity_repinned(slot) == (info.st_dev, info.st_ino)
        assert slot.project_identity == (str(second), info.st_dev, info.st_ino)
        assert calls == [str(first), str(second)]

    def test_a_forged_transcript_line_is_ignored_by_the_restore(self, tmp_path):
        """Nothing identity-bearing is read back from the transcript store: a
        ``project_identity`` line an in-sandbox writer planted beside ``project``
        restores nothing, so the first spawn re-pins the directory as it stands
        instead of trusting the planted pair."""
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import (
            _apply_recent_session,
            _rehydrate_slot_from_history,
        )
        from kiro_crew.dashboard.state import spawn_project_identity

        state = _make_state(tmp_path)
        meta = {"project": str(tmp_path), "project_identity": [str(tmp_path), 7, 11]}
        _apply_recent_session(
            state,
            "chat-1-1700000000",
            "restored",
            {},
            meta,
            [],
            conv_log=state.conversation_log,
            kiro_model_map={},
            restore_cfg=None,
        )
        slot = state._slots["restored"]
        assert slot.project == str(tmp_path)
        assert slot.project_identity is None
        assert spawn_project_identity(slot) is None
        assert _rehydrate_slot_from_history is not None  # the other reader: same contract


def test_the_unavailable_identity_is_a_recorded_state_in_this_process() -> None:
    """A binding on a volume that reports no inode records
    ``IDENTITY_UNAVAILABLE`` -- a state the spawn opens but does not compare --
    never ``None``, for the process's lifetime; ``None`` stays what it is: no
    record in this process, which the first spawn re-pins."""
    from kiro_crew.dashboard.state import (
        _ChatSlot,
        record_project_identity,
        spawn_project_identity,
    )

    slot = _ChatSlot.__new__(_ChatSlot)
    slot.project = "/work/on-a-share"
    slot.project_identity = None
    record_project_identity(slot, IDENTITY_UNAVAILABLE)
    assert slot.project_identity == ("/work/on-a-share", 0, 0)
    assert spawn_project_identity(slot) == IDENTITY_UNAVAILABLE
    record_project_identity(slot, None)
    assert slot.project_identity is None and spawn_project_identity(slot) is None


class TestEveryPathWithoutAVerifiedIdentityRefuses:
    """THE CLASS, stated once: every path that cannot produce a verified
    identity REFUSES -- (a) a pin failure at bind, (b) an identity mismatch at
    spawn, (c) a failed re-pin at restart -- none answers ``None``, none logs
    and continues. ``None`` (the check skipped) is legal ONLY for a slot with no
    binding at all. One pin per path, named for the path."""

    def test_pin_failure_at_bind_refuses(self, tmp_path):
        """(a) The bind's pin of a missing leaf, a file, or a link raises
        ``WorkspacePinFailed``; nothing is recorded (the arms surface it)."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, record_project_identity

        slot = _ChatSlot("dashboard:bind")
        slot.project = str(tmp_path / "gone")
        with pytest.raises(sandbox.WorkspacePinFailed, match="could not be pinned"):
            record_project_identity(slot, sandbox.directory_identity_pinned(slot.project))
        assert slot.project_identity is None
        afile = tmp_path / "file"
        afile.write_text("x")
        with pytest.raises(sandbox.WorkspacePinFailed, match="link or not a directory"):
            sandbox.directory_identity_pinned(afile)

    def test_identity_mismatch_at_spawn_refuses(self, tmp_path):
        """(b) A different directory at the bound name refuses the spawn; the
        governed error names the remedy."""
        from kiro_crew import sandbox

        proj = tmp_path / "proj"
        proj.mkdir()
        info = os.stat(proj)
        other = tmp_path / ".proj.other"
        other.mkdir()
        proj.rmdir()
        other.rename(proj)
        with pytest.raises(
            sandbox.AgentWorkspacePinRefused, match="different directory now sits"
        ) as ei:
            sandbox.verify_agent_workspace_for_spawn(str(proj), (info.st_dev, info.st_ino))
        assert "re-bind the project directory" in str(ei.value)

    @pytest.mark.asyncio
    async def test_failed_repin_at_restart_refuses(self, tmp_path):
        """(c) A bound directory that cannot be re-pinned after a restart -- a
        missing leaf here, a planted link in the runner pin -- raises the
        governed refusal; nothing is recorded, nothing is skipped."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        slot = _ChatSlot("dashboard:restarted")
        slot.project = str(tmp_path / "gone")
        slot.project_identity = None  # what a restart leaves
        with pytest.raises(sandbox.WorkspacePinFailed, match="could not be re-pinned") as ei:
            await spawn_project_identity_repinned(slot)
        assert "re-bind the project directory" in str(ei.value)
        assert slot.project_identity is None

    @pytest.mark.asyncio
    async def test_unbound_slot_skips_the_check(self, tmp_path):
        """A slot with no binding at all is the ONE shape that is not examined:
        no re-pin, ``cwd_identity`` ``None``, and the spawn check passes the
        name through untouched."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        slot = _ChatSlot("dashboard:unbound")
        slot.project = ""
        assert await spawn_project_identity_repinned(slot) is None
        assert sandbox.verify_agent_workspace_for_spawn(str(tmp_path), None) == (
            str(tmp_path),
            None,
        )


class TestBindRepinAndSpawnShareOneIdentityFunction:
    """Bind, restart re-pin and spawn verification share ONE identity function
    per platform (``sandbox.open_pinned_directory``): Windows, the root-first
    no-reparse component walk; POSIX, the ``O_NOFOLLOW | O_DIRECTORY`` leaf open
    + ``fstat``. So the three paths cannot disagree about a link: a junction at
    an ANCESTOR of the bound spelling is refused by all three on Windows without
    being resolved (review-caught: the restart re-pin opened the leaf by name and
    let the kernel resolve the ancestors), a leaf link is refused by all three on
    POSIX, and an unchanged directory passes all three with one identity."""

    @staticmethod
    def _windows_arm(monkeypatch, tmp_path):
        """The Windows arm on every host: on the Windows shard a real junction
        (``_winapi.CreateJunction``) and the real seams; elsewhere the arm through
        its ``platform_compat`` seams -- the no-follow open of a real directory,
        the attribute and tag reads answering the mount-point tag."""
        from kiro_crew import pinned_fs, sandbox

        pc = sandbox.platform_compat
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        real = tmp_path / "real"
        (real / "inside").mkdir(parents=True)
        junction = tmp_path / "junction"
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(real), str(junction))
        else:
            (junction / "inside").mkdir(parents=True)  # the junction object, as a real dir
            monkeypatch.setattr(pc, "IS_POSIX", False)
            monkeypatch.setattr(
                pc, "_win_open_without_following", lambda path: os.open(str(path), os.O_RDONLY)
            )
            reparse = pc._WIN_FILE_ATTRIBUTE_DIRECTORY | pc._WIN_FILE_ATTRIBUTE_REPARSE_POINT
            plain = pc._WIN_FILE_ATTRIBUTE_DIRECTORY
            junction_id = os.stat(junction).st_ino
            monkeypatch.setattr(
                pc,
                "_win_file_attributes",
                lambda fd: reparse if os.fstat(fd).st_ino == junction_id else plain,
            )
            monkeypatch.setattr(pc, "_win_reparse_tag", lambda fd: 0xA0000003)  # MOUNT_POINT
            ident = lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino)  # noqa: E731
            monkeypatch.setattr(pc, "handle_identity", ident)
            monkeypatch.setattr(pinned_fs, "handle_identity", ident)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(pinned_fs, "_windows_handle_pin_available", lambda: True)
        return real / "inside", junction / "inside"

    @pytest.mark.asyncio
    async def test_a_windows_junction_ancestor_is_refused_at_bind_repin_and_spawn(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        unchanged, under_junction = self._windows_arm(monkeypatch, tmp_path)
        # bind
        with pytest.raises(sandbox.WorkspacePinFailed, match="link or not a directory"):
            sandbox.directory_identity_pinned(under_junction)
        # restart re-pin
        slot = _ChatSlot("dashboard:win-repin")
        slot.project = str(under_junction)
        with pytest.raises(sandbox.WorkspacePinFailed, match="could not be re-pinned"):
            await spawn_project_identity_repinned(slot)
        assert slot.project_identity is None
        # spawn
        with pytest.raises(sandbox.AgentWorkspacePinRefused, match="component above it"):
            sandbox.verify_agent_workspace_for_spawn(str(under_junction), (7, 11))
        # the unchanged directory passes all three with ONE identity
        identity = sandbox.directory_identity_pinned(unchanged)
        assert identity not in (None, sandbox.IDENTITY_UNAVAILABLE)
        good = _ChatSlot("dashboard:win-good")
        good.project = str(unchanged)
        assert await spawn_project_identity_repinned(good) == identity
        assert sandbox.verify_agent_workspace_for_spawn(str(unchanged), identity) == (
            str(unchanged),
            None,
        )

    @pytest.mark.asyncio
    async def test_a_posix_leaf_link_is_refused_at_bind_repin_and_spawn(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            # No link granted on this host: the kernel's answer to a no-follow
            # open of a link (ELOOP), handed to THE identity read.
            link.mkdir()
            monkeypatch.setattr(
                sandbox,
                "open_pinned_directory",
                lambda path: (_ for _ in ()).throw(OSError(errno.ELOOP, "link", path)),
            )
        # bind
        with pytest.raises(sandbox.WorkspacePinFailed, match="link or not a directory"):
            sandbox.directory_identity_pinned(link)
        # restart re-pin
        slot = _ChatSlot("dashboard:posix-repin")
        slot.project = str(link)
        with pytest.raises(sandbox.WorkspacePinFailed, match="could not be re-pinned"):
            await spawn_project_identity_repinned(slot)
        # spawn
        with pytest.raises(sandbox.AgentWorkspacePinRefused, match="now a link"):
            sandbox.verify_agent_workspace_for_spawn(str(link), (7, 11))
        # the unchanged directory passes all three with ONE identity
        monkeypatch.undo()
        identity = sandbox.directory_identity_pinned(target)
        info = os.stat(target)
        assert identity == (info.st_dev, info.st_ino)
        good = _ChatSlot("dashboard:posix-good")
        good.project = str(target)
        assert await spawn_project_identity_repinned(good) == identity
        real, fd = sandbox.verify_agent_workspace_for_spawn(str(target), identity)
        sandbox.release_agent_workspace_fd(fd)
        assert os.path.realpath(real) == os.path.realpath(target)
