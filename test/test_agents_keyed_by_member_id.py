"""``config.agents`` is keyed by ``member_id``; ``display_name`` is a field.

A legacy document keys the map by the crew's display name with the stable id
inside the record. These tests pin the keyed shape:

* MIGRATION -- an old-shape document (key = display name, ``member_id`` inside)
  loads re-keyed by the id with the old key kept as ``display_name``; the
  load writes nothing and the one-shot ``migrate_member_identity`` is idempotent; an id another entry already uses as its key is
  refused, not guessed; ``default_agent`` follows the re-key.
* RENAME -- ``PUT /api/agents/{name}`` with ``display_name`` edits the field
  only: the key, ``member_id``, the DM slot key and the memory store stay.
* ROSTER -- ``GET /api/agents`` and ``GET /api/members`` carry ``member_id``,
  ``display_name`` and ``name`` (an alias of ``display_name`` for one release).
* LOOKUP -- every handle route resolves the key AND the display name through
  ``members.resolve_member``; an unknown handle is 404.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew import members as members_mod
from kiro_crew.config import loader as loader_module
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _config_path() -> Path:
    return config_dir() / "config.json"


def _write_config(data: dict) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    loader_module._invalidate_config_cache()
    return path


def _old_shape() -> dict:
    """A legacy document: agents keyed by display name, ids inside."""
    return {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
            "Crew Program Manager": {
                "member_id": "crew-program-manager",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "member-cpm",
            },
            "Dr. Eggbot": {
                "member_id": "dr-eggbot",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "member-egg",
                "display_name": "Doctor Eggbot",
            },
        },
        "default_agent": "Crew Program Manager",
        "workspaces": {"default": {"dir": "workspace"}},
        "memory_stores": {
            "default": {},
            "member-cpm": {
                "owner_member": "Crew Program Manager",
                "owner_member_id": "crew-program-manager",
                "memory_version": 2,
            },
            "member-egg": {
                "owner_member": "Dr. Eggbot",
                "owner_member_id": "dr-eggbot",
                "memory_version": 2,
            },
        },
    }


class TestPlan:
    """The pure planner both halves of the migration share."""

    def test_rekeys_entries_whose_id_differs_from_their_key(self):
        plan = loader_module.plan_member_rekeys(
            [("Crew Program Manager", "crew-program-manager", ""), ("default", "", "")]
        )
        assert plan == {"Crew Program Manager": "crew-program-manager"}

    def test_entry_without_id_keeps_its_key(self):
        assert loader_module.plan_member_rekeys([("legacy", "", ""), ("other", None, "")]) == {}

    def test_id_already_used_as_a_key_is_refused(self, caplog):
        with caplog.at_level(logging.WARNING):
            plan = loader_module.plan_member_rekeys([("Writer", "writer", ""), ("writer", "", "")])
        assert plan == {}
        assert "cannot become their key" in caplog.text

    def test_id_claimed_by_two_entries_is_refused(self):
        plan = loader_module.plan_member_rekeys([("A", "shared", ""), ("B", "shared", "")])
        assert plan == {}

    def test_refused_move_does_not_vacate_its_key(self, caplog):
        # ``A`` and ``B`` both claim ``shared`` and are refused, so ``A`` STAYS
        # a key; ``X`` claiming ``A`` must be refused too, or ``X`` and ``A``
        # would be written under one key and one of them dropped.
        with caplog.at_level(logging.WARNING):
            plan = loader_module.plan_member_rekeys(
                [("A", "shared", ""), ("B", "shared", ""), ("X", "A", "")]
            )
        assert plan == {}
        assert "already a key that stays" in caplog.text

    def test_refusal_cascades_along_a_chain_onto_a_staying_key(self):
        # ``b -> c`` is refused because ``c`` stays; ``a -> b`` then finds ``b``
        # staying as well and is refused in turn.
        plan = loader_module.plan_member_rekeys([("a", "b", ""), ("b", "c", ""), ("c", "", "")])
        assert plan == {}
        # The same chain with ``c`` moving away lands every entry on its id.
        plan = loader_module.plan_member_rekeys([("a", "b", ""), ("b", "c", ""), ("c", "d", "")])
        assert plan == {"a": "b", "b": "c", "c": "d"}

    def test_document_rekey_never_drops_an_entry(self):
        agents = {
            "A": {"member_id": "shared"},
            "B": {"member_id": "shared"},
            "X": {"member_id": "A"},
        }
        rekeyed, plan = loader_module.rekey_agents_document(agents)
        assert plan == {}
        assert rekeyed == agents and len(rekeyed) == 3


class TestLoadMigration:
    def test_old_shape_loads_keyed_by_id_with_old_key_as_display_name(self):
        _write_config(_old_shape())
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "crew-program-manager", "dr-eggbot"}
        cpm = cfg.agents["crew-program-manager"]
        assert cpm.member_id == "crew-program-manager"
        assert cpm.display_name == "Crew Program Manager"
        # A display_name already set is kept; the old key is not forced over it.
        assert cfg.agents["dr-eggbot"].display_name == "Doctor Eggbot"
        # default_agent followed the re-key rather than being reassigned.
        assert cfg.default_agent == "crew-program-manager"

    def test_load_is_read_only_and_the_migration_lands_once(self):
        path = _write_config(_old_shape())
        before = path.read_text(encoding="utf-8")
        # A load serves the keyed shape in memory and writes nothing for it.
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "crew-program-manager", "dr-eggbot"}
        assert path.read_text(encoding="utf-8") == before
        # The one-shot moves the document, once.
        moved = loader_module.migrate_member_identity()
        assert moved["rekeyed"] == 2
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert set(on_disk["agents"]) == {"default", "crew-program-manager", "dr-eggbot"}
        assert on_disk["agents"]["crew-program-manager"]["display_name"] == "Crew Program Manager"
        assert on_disk["default_agent"] == "crew-program-manager"
        # Identity bindings are untouched by the re-key.
        assert on_disk["memory_stores"]["member-cpm"]["owner_member_id"] == "crew-program-manager"
        first = path.read_text(encoding="utf-8")
        loader_module._invalidate_config_cache()
        assert loader_module.migrate_member_identity()["rekeyed"] == 0
        assert path.read_text(encoding="utf-8") == first

    def test_member_update_patches_the_legacy_record_before_the_migration(self):
        # Between a load (read-only) and the one-shot migration the document
        # still stores the member under its legacy key; an update addressed by
        # the id must land on THAT record, not add a second one beside it.
        from kiro_crew.memory_stores import persist_member_config

        path = _write_config(_old_shape())
        cfg = KiroCrewConfig.load()
        cfg.agents["crew-program-manager"].model = "pinned"
        persist_member_config(
            cfg,
            "crew-program-manager",
            create=False,
            expected_store="member-cpm",
            changed_fields={"model"},
        )
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert "crew-program-manager" not in on_disk["agents"]
        assert on_disk["agents"]["Crew Program Manager"]["model"] == "pinned"
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().agents["crew-program-manager"].model == "pinned"

    def test_new_shape_is_not_rewritten(self):
        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "crew-program-manager": {
                **data["agents"]["Crew Program Manager"],
                "display_name": "Crew Program Manager",
            },
        }
        data["default_agent"] = "crew-program-manager"
        del data["memory_stores"]["member-egg"]
        path = _write_config(data)
        before = path.read_text(encoding="utf-8")
        cfg = KiroCrewConfig.load()
        assert path.read_text(encoding="utf-8") == before
        assert cfg.agents["crew-program-manager"].display_name == "Crew Program Manager"

    def test_colliding_id_stays_where_stored(self):
        data = _old_shape()
        # ``writer`` is both a legacy key and another entry's id: refused.
        data["agents"]["writer"] = {"kiro_agent": "kirocrew"}
        data["agents"]["Writer"] = {"member_id": "writer", "kiro_agent": "kirocrew"}
        _write_config(data)
        cfg = KiroCrewConfig.load()
        assert "Writer" in cfg.agents and "writer" in cfg.agents
        assert cfg.agents["Writer"].member_id == "writer"


class TestResolver:
    def test_resolves_by_key_and_by_display_name(self):
        _write_config(_old_shape())
        cfg = KiroCrewConfig.load()
        by_id = members_mod.resolve_member("crew-program-manager", cfg)
        by_name = members_mod.resolve_member("Crew Program Manager", cfg)
        assert by_id is not None and by_id == by_name
        assert by_id[0] == "crew-program-manager"
        assert members_mod.resolve_member_id("Doctor Eggbot", cfg) == "dr-eggbot"
        assert members_mod.resolve_member("nobody", cfg) is None
        assert members_mod.resolve_member("", cfg) is None

    def test_key_wins_over_a_display_name_spelling(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["alpha"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="beta")
        cfg.agents["beta"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="alpha")
        assert members_mod.resolve_member_id("alpha", cfg) == "alpha"
        assert members_mod.resolve_member_id("beta", cfg) == "beta"

    def test_shared_display_name_is_refused(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["one"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Same")
        cfg.agents["two"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Same")
        assert members_mod.resolve_member("Same", cfg) is None
        assert members_mod.same_member("one", "two", cfg) is False

    def test_member_slug_uses_the_id_for_either_handle(self):
        _write_config(_old_shape())
        cfg = KiroCrewConfig.load()
        assert members_mod.member_slug("Crew Program Manager", cfg) == "crew-program-manager"
        assert members_mod.member_slug("crew-program-manager", cfg) == "crew-program-manager"

    def test_allocator_reserves_every_key(self):
        from kiro_crew.memory_stores import _allocate_member_id

        cfg = KiroCrewConfig.load()
        cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        allocated = _allocate_member_id(cfg, "Writer")
        assert allocated != "writer" and allocated.startswith("writer-")
        # The entry's own key is exempt: a record already keyed by its id keeps it.
        assert _allocate_member_id(cfg, "writer") == "writer"

    def test_allocator_reserves_every_display_name(self):
        # ``resolve_member`` prefers a key over a label: were the new member
        # keyed ``writer``, every request addressed to the label ``writer``
        # would route to it instead of ``author``.
        from kiro_crew.memory_stores import _allocate_member_id

        cfg = KiroCrewConfig.load()
        cfg.agents["author"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="writer")
        allocated = _allocate_member_id(cfg, "Writer!")
        assert allocated != "writer" and allocated.startswith("writer-")
        assert members_mod.resolve_member_id("writer", cfg) == "author"
        # The entry's own label is exempt, like its own key.
        cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="writer")
        del cfg.agents["author"]
        assert _allocate_member_id(cfg, "writer") == "writer"


def _agents_app(tmp_path) -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_delete,
        api_kirocrew_agent_update,
        api_kirocrew_agents,
    )
    from kiro_crew.dashboard.handlers.members import api_members

    app = web.Application()
    app["state"] = _make_state(tmp_path)
    app.router.add_get("/api/agents", api_kirocrew_agents)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    app.router.add_delete("/api/agents/{name}", api_kirocrew_agent_delete)
    app.router.add_get("/api/members", api_members)
    return app


@pytest.fixture()
def keyed_member():
    """A new-shape member with a private V2 store on disk (identity to protect)."""
    from kiro_crew.memory_stores import persist_member_config, provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents["release-writer"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", display_name="Release Writer"
    )
    provision_member_memory(cfg, "release-writer")
    persist_member_config(cfg, "release-writer", create=True)
    cfg = KiroCrewConfig.load()
    assert cfg.agents["release-writer"].member_id == "release-writer"
    return cfg.agents["release-writer"]


class TestRosters:
    @pytest.mark.asyncio
    async def test_agents_rows_carry_id_label_and_alias(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/agents")).json()
        row = next(a for a in body["agents"] if a["member_id"] == "release-writer")
        assert row["display_name"] == "Release Writer"
        assert row["name"] == "Release Writer"
        # A record with no label shows its key in every slot.
        default = next(a for a in body["agents"] if a["member_id"] == "default")
        assert default["name"] == default["display_name"] == "default"

    @pytest.mark.asyncio
    async def test_members_rows_carry_id_label_and_alias(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/members")).json()
        row = next(m for m in body["members"] if m["member_id"] == "release-writer")
        assert row["display_name"] == "Release Writer"
        assert row["name"] == "Release Writer"
        assert row["slug"] == "release-writer"


class TestRename:
    @pytest.mark.asyncio
    async def test_rename_edits_the_field_and_keeps_identity(self, tmp_path, keyed_member):
        before = keyed_member
        slug = members_mod.member_slug("release-writer")
        members_mod.write_dm_binding(
            slug,
            member="release-writer",
            slot_key=members_mod.member_slot_key(slug, before.memory_store),
            memory_store=before.memory_store,
        )
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put(
                "/api/agents/Release Writer", json={"display_name": "Release Author"}
            )
            assert resp.status == 200
            data = await resp.json()
        assert data["member_id"] == "release-writer"
        assert data["display_name"] == data["name"] == "Release Author"
        cfg = KiroCrewConfig.load()
        assert "release-writer" in cfg.agents and "Release Author" not in cfg.agents
        after = cfg.agents["release-writer"]
        assert after.member_id == "release-writer"
        assert after.memory_store == before.memory_store
        assert after.display_name == "Release Author"
        assert members_mod.member_slug("Release Author", cfg) == slug
        binding = members_mod.read_dm_binding(slug)
        assert binding is not None and binding["slot_key"] == members_mod.member_slot_key(
            slug, before.memory_store
        )

    @pytest.mark.asyncio
    async def test_rename_to_another_members_handle_is_refused(self, tmp_path, keyed_member):
        cfg = KiroCrewConfig.load()
        cfg.agents["other"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Other")
        cfg.save()
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            for taken in ("other", "Other"):
                resp = await client.put("/api/agents/release-writer", json={"display_name": taken})
                assert resp.status == 409
                assert (await resp.json())["code"] == "agent_exists"
        assert KiroCrewConfig.load().agents["release-writer"].display_name == "Release Writer"

    @pytest.mark.asyncio
    async def test_rename_validates_like_create(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            resp = await client.put("/api/agents/release-writer", json={"display_name": "a\tb"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_member_name"


class TestHandleRoutes:
    @pytest.mark.asyncio
    async def test_update_by_id_and_by_display_name_hit_one_record(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            assert (
                await client.put("/api/agents/release-writer", json={"description": "by id"})
            ).status == 200
            assert KiroCrewConfig.load().agents["release-writer"].description == "by id"
            assert (
                await client.put("/api/agents/Release Writer", json={"description": "by name"})
            ).status == 200
            assert KiroCrewConfig.load().agents["release-writer"].description == "by name"

    @pytest.mark.asyncio
    async def test_unknown_handle_is_404(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            assert (await client.put("/api/agents/nobody", json={"description": "x"})).status == 404
            assert (await client.delete("/api/agents/nobody")).status == 404

    @pytest.mark.asyncio
    async def test_delete_by_display_name(self, tmp_path, keyed_member):
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            assert (await client.delete("/api/agents/Release Writer")).status == 200
        assert "release-writer" not in KiroCrewConfig.load().agents

    def test_dispatch_resolves_either_handle(self, keyed_member):
        cfg = KiroCrewConfig.load()
        by_id = loader_module.resolve_agent_identity(cfg, "release-writer")
        by_name = loader_module.resolve_agent_identity(cfg, "Release Writer")
        assert by_id == by_name
        assert by_id[0] == "release-writer"
        assert loader_module.resolve_crew_identity(cfg, "Release Writer", None) == "release-writer"
        assert loader_module.resolve_crew_identity(cfg, None, "Release Writer") == "release-writer"


class TestOverlayStaysOneMember:
    """``config.local.json`` is never written back, so a member it patches can
    stay filed under the display-name key for good; merging that onto the
    id-keyed base must not surface a second, partial member."""

    def _overlay_path(self) -> Path:
        from kiro_crew.config.loader import config_local_path

        return config_local_path()

    def test_overlay_patch_under_the_legacy_key_folds_onto_the_member(self):
        _write_config(_old_shape())
        overlay = {"agents": {"Crew Program Manager": {"model": "overlay-model"}}}
        self._overlay_path().write_text(json.dumps(overlay), encoding="utf-8")
        loader_module._invalidate_config_cache()
        loader_module.migrate_member_identity()
        cfg = KiroCrewConfig.load()
        assert "Crew Program Manager" not in cfg.agents
        assert cfg.agents["crew-program-manager"].model == "overlay-model"
        assert members_mod.resolve_member_id("Crew Program Manager", cfg) == "crew-program-manager"
        # Only the base document is migrated; the user-owned overlay is untouched
        # and still folds onto the member on the next load.
        assert json.loads(self._overlay_path().read_text(encoding="utf-8")) == overlay
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        assert "Crew Program Manager" not in on_disk["agents"]
        loader_module._invalidate_config_cache()
        again = KiroCrewConfig.load()
        assert "Crew Program Manager" not in again.agents
        assert again.agents["crew-program-manager"].model == "overlay-model"

    def test_overlay_under_the_legacy_key_of_an_explicitly_labelled_member_stays_attached(self):
        # ``Dr. Eggbot`` carried its own display_name (``Doctor Eggbot``), so the
        # old key survives only as the record's ``legacy_keys``; the overlay,
        # never rewritten, still patches ``Dr. Eggbot`` and must keep folding
        # onto ``dr-eggbot`` on every later load instead of surfacing as a
        # second, partial member.
        _write_config(_old_shape())
        overlay = {"agents": {"Dr. Eggbot": {"model": "overlay-model"}}}
        self._overlay_path().write_text(json.dumps(overlay), encoding="utf-8")
        loader_module._invalidate_config_cache()
        loader_module.migrate_member_identity()
        first = KiroCrewConfig.load()
        assert first.agents["dr-eggbot"].model == "overlay-model"
        assert first.agents["dr-eggbot"].legacy_keys == ["Dr. Eggbot"]
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        assert set(on_disk["agents"]) == {"default", "crew-program-manager", "dr-eggbot"}
        assert on_disk["agents"]["dr-eggbot"]["display_name"] == "Doctor Eggbot"
        assert on_disk["agents"]["dr-eggbot"]["legacy_keys"] == ["Dr. Eggbot"]
        loader_module._invalidate_config_cache()
        again = KiroCrewConfig.load()
        assert set(again.agents) == {"default", "crew-program-manager", "dr-eggbot"}
        assert again.agents["dr-eggbot"].model == "overlay-model"
        assert again.agents["dr-eggbot"].display_name == "Doctor Eggbot"
        # The resolver, the default-agent canonicalizer and the capability
        # service's overlay view all honour the remembered key.
        assert members_mod.resolve_member_id("Dr. Eggbot", again) == "dr-eggbot"
        assert loader_module.canonical_agent_key("Dr. Eggbot", on_disk["agents"], {}) == "dr-eggbot"
        from kiro_crew.agent_capabilities import _canonical_overlay_agents

        assert _canonical_overlay_agents(on_disk, overlay) == {
            "dr-eggbot": {"model": "overlay-model"}
        }

    def test_a_live_handle_beats_a_remembered_legacy_key(self):
        # Once another member takes the retired spelling as key or label, the
        # overlay entry under it is THAT member's patch -- the memory yields;
        # a key two records both remember answers nobody.
        _write_config(_old_shape())
        loader_module._invalidate_config_cache()
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        agents = on_disk["agents"]
        assert loader_module.legacy_key_aliases(agents) == {"Dr. Eggbot": "dr-eggbot"}
        # ``Crew Program Manager`` became the member's label, so the label owns it.
        assert agents["crew-program-manager"]["legacy_keys"] == ["Crew Program Manager"]
        agents["newcomer"] = {
            "member_id": "newcomer",
            "kiro_agent": "kirocrew",
            "display_name": "Dr. Eggbot",
        }
        assert loader_module.legacy_key_aliases(agents) == {}
        cfg = KiroCrewConfig.load()
        cfg.agents["newcomer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="newcomer", display_name="Dr. Eggbot"
        )
        assert members_mod.resolve_member_id("Dr. Eggbot", cfg) == "newcomer"
        del agents["newcomer"]
        agents["twin"] = {
            "member_id": "twin",
            "kiro_agent": "kirocrew",
            "legacy_keys": ["Dr. Eggbot"],
        }
        assert loader_module.legacy_key_aliases(agents) == {}
        del cfg.agents["newcomer"]
        cfg.agents["twin"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="twin", legacy_keys=["Dr. Eggbot"]
        )
        assert members_mod.resolve_member("Dr. Eggbot", cfg) is None
        # A malformed stored list is read as clean strings, never raised on.
        assert loader_module._legacy_keys_field(["a", 1, None, "a", ""]) == ["a"]
        assert loader_module._legacy_keys_field("a") == []

    def test_allocator_and_create_reserve_a_remembered_legacy_key(self):
        from kiro_crew.memory_stores import _allocate_member_id

        cfg = KiroCrewConfig.load()
        cfg.agents["author"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="author", display_name="Author", legacy_keys=["writer"]
        )
        assert _allocate_member_id(cfg, "Writer").startswith("writer-")
        assert members_mod.resolve_member_id("writer", cfg) == "author"

    def test_raw_agent_key_files_a_patch_where_the_record_is(self):
        base = {
            "agents": {
                "Writer": {"member_id": "writer", "kiro_agent": "k"},
                "Dr. Eggbot": {"member_id": "dr-eggbot", "display_name": "Doctor Eggbot"},
                "plain": {"kiro_agent": "k"},
            }
        }
        assert loader_module.raw_agent_key(base, "writer") == "Writer"
        assert loader_module.raw_agent_key(base, "dr-eggbot") == "Dr. Eggbot"
        assert loader_module.raw_agent_key(base, "plain") == "plain"
        assert loader_module.raw_agent_key(base, "newcomer") == "newcomer"
        overlay = {"agents": {"Writer": {"model": "x"}, "Doctor Eggbot": {"model": "y"}}}
        assert loader_module.raw_agent_key(overlay, "writer", base=base) == "Writer"
        assert loader_module.raw_agent_key(overlay, "dr-eggbot", base=base) == "Doctor Eggbot"
        assert loader_module.raw_agent_key(overlay, "plain", base=base) == "plain"
        assert loader_module.raw_agent_key({}, "writer", base=base) == "writer"

    def test_merge_without_a_plan_honours_remembered_legacy_keys(self):
        # The capability service merges without the loader's plan; an overlay
        # entry under a REMEMBERED legacy key of an explicitly labelled,
        # already-migrated member still folds onto that member.
        _write_config(_old_shape())
        loader_module.migrate_member_identity()
        on_disk = json.loads(_config_path().read_text(encoding="utf-8"))
        merged = loader_module.merge_config_documents(
            on_disk, {"agents": {"Dr. Eggbot": {"model": "overlay-model"}}}
        )
        assert "Dr. Eggbot" not in merged["agents"]
        assert merged["agents"]["dr-eggbot"]["model"] == "overlay-model"

    def test_overlay_patch_by_display_name_after_the_base_migrated(self):
        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "crew-program-manager": {
                **data["agents"]["Crew Program Manager"],
                "display_name": "Crew Program Manager",
            },
        }
        data["default_agent"] = "crew-program-manager"
        del data["memory_stores"]["member-egg"]
        _write_config(data)
        overlay = {"agents": {"Crew Program Manager": {"triggers": "from overlay"}}}
        self._overlay_path().write_text(json.dumps(overlay), encoding="utf-8")
        loader_module._invalidate_config_cache()
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "crew-program-manager"}
        assert cfg.agents["crew-program-manager"].triggers == "from overlay"

    def test_merge_config_documents_canonicalizes_both_sides(self):
        base = {
            "agents": {"Writer": {"member_id": "writer", "kiro_agent": "kirocrew"}},
            "default_agent": "Writer",
        }
        overlay = {
            "agents": {
                "Writer": {"model": "a"},
                "writer": {"triggers": "b"},
                "Only Here": {"kiro_agent": "kirocrew"},
            }
        }
        merged = loader_module.merge_config_documents(base, overlay)
        assert set(merged["agents"]) == {"writer", "Only Here"}
        assert merged["agents"]["writer"] == {
            "member_id": "writer",
            "kiro_agent": "kirocrew",
            "display_name": "Writer",
            "legacy_keys": ["Writer"],
            "model": "a",
            "triggers": "b",
        }
        # The merge returns copies: the base document handed in is not migrated.
        assert "Writer" in base["agents"]


class TestTeamsHoldOneIdentityPerMember:
    def _app(self) -> web.Application:
        from kiro_crew.dashboard.handlers.teams import (
            api_teams_create,
            api_teams_list,
            api_teams_update,
        )

        @web.middleware
        async def _auth(request: web.Request, handler):
            request["app"] = ""
            return await handler(request)

        app = web.Application(middlewares=[_auth])
        app.router.add_get("/api/teams", api_teams_list)
        app.router.add_post("/api/teams", api_teams_create)
        app.router.add_put("/api/teams/{id}", api_teams_update)
        return app

    @pytest.mark.asyncio
    async def test_id_and_display_name_alias_store_one_member(self, monkeypatch, keyed_member):
        from unittest.mock import AsyncMock

        from kiro_crew import crew_teams

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.teams.require_owner_dashboard_request",
            AsyncMock(return_value=None),
        )
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.post(
                "/api/teams",
                json={"name": "Docs", "members": ["release-writer", "Release Writer"]},
            )
            assert resp.status == 201, await resp.text()
            team = (await resp.json())["team"]
            # Stored once, by the key; answered by the roster's handle.
            assert crew_teams.read_teams()[0].members == ["release-writer"]
            assert team["members"] == ["Release Writer"]
            # The one-team invariant holds across spellings: naming the member by
            # its display name on a second team moves it off the first.
            resp = await client.post(
                "/api/teams", json={"name": "Ops", "members": ["Release Writer"]}
            )
            assert resp.status == 201
            stored = {t.name: t.members for t in crew_teams.read_teams()}
            assert stored == {"Docs": [], "Ops": ["release-writer"]}
            # ``add`` / ``remove`` deltas canonicalize the same way.
            ops_id = next(t.id for t in crew_teams.read_teams() if t.name == "Ops")
            resp = await client.put(f"/api/teams/{ops_id}", json={"remove": ["Release Writer"]})
            assert resp.status == 200
            assert (await resp.json())["team"]["members"] == []
            listing = await (await client.get("/api/teams")).json()
            assert {t["name"]: t["members"] for t in listing["teams"]} == {"Docs": [], "Ops": []}

    def test_a_legacy_document_listing_display_names_reads_as_the_key(self, keyed_member):
        from kiro_crew import crew_teams

        cfg = KiroCrewConfig.load()
        teams = [crew_teams.Team(id="aaaaaaaaaaaa", name="Docs", members=["Release Writer"])]
        canonical = crew_teams.canonicalize_teams(
            teams, lambda m: members_mod.resolve_member_id(m, cfg) or m
        )
        assert canonical[0].members == ["release-writer"]


class TestPrivateCopyOwnershipIsById:
    """A private template copy's ``private_to`` is the owner's ``config.agents``
    key. Labels are reusable, so an owner recorded by label would hand the copy
    to whichever member wears the label next."""

    def test_load_rewrites_legacy_label_owners_with_the_agents_rekey(self):
        from kiro_crew import agent_state

        data = _old_shape()
        data["agents"]["Crew Program Manager"]["kiro_agent"] = "cpm-copy"
        _write_config(data)
        agent_state.set_fork_info(
            "cpm-copy", forked_from="kirocrew", private_to="Crew Program Manager"
        )
        agent_state.set_fork_info("other-copy", forked_from="kirocrew", private_to="somebody-else")
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        assert agent_state.get_fork_info("cpm-copy")["private_to"] == "crew-program-manager"
        assert agent_state.get_fork_info("other-copy")["private_to"] == "somebody-else"
        # Idempotent: a second load rewrites nothing.
        loader_module._invalidate_config_cache()
        KiroCrewConfig.load()
        assert agent_state.get_fork_info("cpm-copy")["private_to"] == "crew-program-manager"

    def test_a_label_owner_is_never_this_member(self):
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers.agents import _foreign_private_copy_owner

        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"] = KiroCrewAgentConfig(
            kiro_agent="rw-copy", display_name="Release Writer"
        )
        cfg.save()
        agent_state.set_fork_info("rw-copy", forked_from="kirocrew", private_to="Release Writer")
        # Recorded by label: foreign to everyone, the labelled member included.
        assert _foreign_private_copy_owner("release-writer", "rw-copy") == "Release Writer"
        assert _foreign_private_copy_owner("someone", "rw-copy") == "Release Writer"
        agent_state.set_fork_info("rw-copy", forked_from="kirocrew", private_to="release-writer")
        assert _foreign_private_copy_owner("release-writer", "rw-copy") is None
        assert _foreign_private_copy_owner("someone", "rw-copy") == "release-writer"

    @pytest.mark.asyncio
    async def test_reused_label_does_not_inherit_the_renamed_members_copy(
        self, tmp_path, keyed_member
    ):
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers.agents import _foreign_private_copy_owner

        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].kiro_agent = "rw-copy"
        cfg.save()
        # A sidecar the load could not repair still names the member by label.
        agent_state.set_fork_info("rw-copy", forked_from="kirocrew", private_to="Release Writer")
        app = _agents_app(tmp_path)
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            # Renaming repairs the member's own copy to its key first ...
            resp = await client.put(
                "/api/agents/release-writer", json={"display_name": "Release Author"}
            )
            assert resp.status == 200
            assert agent_state.get_fork_info("rw-copy")["private_to"] == "release-writer"
            # ... so a new member taking the old label owns nothing of it.
            resp = await client.post(
                "/api/agents", json={"name": "Release Writer", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200, await resp.text()
            newcomer = (await resp.json())["member_id"]
            assert newcomer != "release-writer"
            assert _foreign_private_copy_owner(newcomer, "rw-copy") == "release-writer"
            resp = await client.put(f"/api/agents/{newcomer}", json={"kiro_agent": "rw-copy"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "foreign_private_copy"


class TestDefaultAgentFollowsTheMember:
    def _overlay_path(self) -> Path:
        from kiro_crew.config.loader import config_local_path

        return config_local_path()

    def test_overlay_default_by_legacy_label_selects_the_migrated_member(self):
        _write_config(_old_shape())
        self._overlay_path().write_text(
            json.dumps({"default_agent": "Crew Program Manager"}), encoding="utf-8"
        )
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().default_agent == "crew-program-manager"
        # The base has migrated (the legacy key is now the display name); the
        # untouched overlay still names the same member.
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().default_agent == "crew-program-manager"
        # By an explicit display name too.
        self._overlay_path().write_text(
            json.dumps({"default_agent": "Doctor Eggbot"}), encoding="utf-8"
        )
        loader_module._invalidate_config_cache()
        assert KiroCrewConfig.load().default_agent == "dr-eggbot"

    def test_unresolvable_overlay_default_is_reported_not_silent(self, caplog):
        _write_config(_old_shape())
        self._overlay_path().write_text(
            json.dumps({"default_agent": "nobody-here"}), encoding="utf-8"
        )
        loader_module._invalidate_config_cache()
        with caplog.at_level(logging.WARNING):
            cfg = KiroCrewConfig.load()
        assert cfg.default_agent == "default"
        assert "default_agent 'nobody-here' names no Crew Member" in caplog.text

    def test_base_default_by_display_name_is_written_back_as_the_key(self):
        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "dr-eggbot": {**data["agents"]["Dr. Eggbot"]},
        }
        data["default_agent"] = "Doctor Eggbot"
        del data["memory_stores"]["member-cpm"]
        path = _write_config(data)
        assert KiroCrewConfig.load().default_agent == "dr-eggbot"
        assert json.loads(path.read_text(encoding="utf-8"))["default_agent"] == "dr-eggbot"

    def test_merge_config_documents_maps_default_agent(self):
        base = {
            "agents": {"Writer": {"member_id": "writer", "kiro_agent": "kirocrew"}},
            "default_agent": "Writer",
        }
        assert loader_module.merge_config_documents(base, {})["default_agent"] == "writer"
        merged = loader_module.merge_config_documents(base, {"default_agent": "Writer"})
        assert merged["default_agent"] == "writer"
        merged = loader_module.merge_config_documents(base, {"default_agent": "nobody"})
        assert merged["default_agent"] == "nobody"

    @pytest.mark.asyncio
    async def test_default_agent_route_accepts_either_handle(self, tmp_path, keyed_member):
        from kiro_crew.dashboard.handlers import api_default_agent, api_kirocrew_agents

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_route("*", "/api/config/default-agent", api_default_agent)
        app.router.add_get("/api/agents", api_kirocrew_agents)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/config/default-agent", json={"agent": "Release Writer"})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["default_agent"] == "release-writer"
            assert body["name"] == "Release Writer"
            assert KiroCrewConfig.load().default_agent == "release-writer"
            roster = await (await client.get("/api/agents")).json()
            # Spelled like the rows' ``name``, so the picker's marker matches.
            assert roster["default_agent"] == "Release Writer"
            assert roster["default_member_id"] == "release-writer"
            assert any(a["name"] == roster["default_agent"] for a in roster["agents"])
            resp = await client.put("/api/config/default-agent", json={"agent": "nobody"})
            assert resp.status == 400


class TestRenameMovesTheAvatarOrNothing:
    _PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + b"\x00\x00\x00\x00IEND\xaeB`\x82"

    async def _wear_picture(self, client, handle: str) -> tuple[str, Path]:
        from aiohttp import FormData

        from kiro_crew.dashboard.handlers.agents import _avatar_stem, _avatars_dir

        form = FormData()
        form.add_field("file", self._PNG, filename="face.png", content_type="image/png")
        up = await client.post(f"/api/agents/{handle}/avatar", data=form)
        assert up.status == 200, await up.text()
        token = (await up.json())["token"]
        resp = await client.put(
            f"/api/agents/{handle}",
            json={"avatar": {"kind": "image", "promote": True, "token": token}},
        )
        assert resp.status == 200, await resp.text()
        pin = KiroCrewConfig.load().agents["release-writer"].avatar["file"]
        return pin, _avatars_dir() / f"{_avatar_stem(handle)}.{pin}"

    def _app(self, tmp_path) -> web.Application:
        from kiro_crew.dashboard.handlers import (
            api_kirocrew_agent_avatar_get,
            api_kirocrew_agent_avatar_upload,
        )

        app = _agents_app(tmp_path)
        app.router.add_post("/api/agents/{name}/avatar", api_kirocrew_agent_avatar_upload)
        app.router.add_get("/api/agents/{name}/avatar", api_kirocrew_agent_avatar_get)
        return app

    @pytest.mark.asyncio
    async def test_rename_moves_the_picture_with_the_label(self, tmp_path, keyed_member):
        from kiro_crew.dashboard.handlers.agents import _avatar_stem, _avatars_dir

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, old_path = await self._wear_picture(client, "Release Writer")
            resp = await client.put(
                "/api/agents/release-writer", json={"display_name": "Release Author"}
            )
            assert resp.status == 200
            assert not old_path.exists()
            assert (_avatars_dir() / f"{_avatar_stem('Release Author')}.{pin}").is_file()
            got = await client.get("/api/agents/Release Author/avatar")
            assert got.status == 200
            assert await got.read() == self._PNG

    @pytest.mark.asyncio
    async def test_failed_move_renames_nothing(self, tmp_path, keyed_member, monkeypatch):
        import kiro_crew.dashboard.handlers.agents as handlers

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, old_path = await self._wear_picture(client, "Release Writer")

            def _refuse(src, dst):
                raise PermissionError("read-only avatars directory")

            with monkeypatch.context() as patched:
                patched.setattr(handlers, "replace_with_retry", _refuse)
                resp = await client.put(
                    "/api/agents/release-writer", json={"display_name": "Release Author"}
                )
                assert resp.status == 503
                assert (await resp.json())["code"] == "avatar_move_failed"
            after = KiroCrewConfig.load().agents["release-writer"]
            assert after.display_name == "Release Writer"
            assert after.avatar["file"] == pin
            assert old_path.is_file()
            got = await client.get("/api/agents/Release Writer/avatar")
            assert got.status == 200

    @pytest.mark.asyncio
    async def test_failed_sidecar_claim_moves_the_picture_back(
        self, tmp_path, keyed_member, monkeypatch
    ):
        from kiro_crew import agent_state

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, old_path = await self._wear_picture(client, "Release Writer")

            def _unreadable(*args, **kwargs):
                raise OSError("sidecar unreadable")

            with monkeypatch.context() as patched:
                patched.setattr(agent_state, "claim_private_owner", _unreadable)
                resp = await client.put(
                    "/api/agents/release-writer", json={"display_name": "Release Author"}
                )
                assert resp.status >= 500
            after = KiroCrewConfig.load().agents["release-writer"]
            assert after.display_name == "Release Writer"
            assert after.avatar["file"] == pin
            assert old_path.is_file()
            got = await client.get("/api/agents/Release Writer/avatar")
            assert got.status == 200

    @pytest.mark.asyncio
    async def test_failed_config_write_moves_the_picture_back(
        self, tmp_path, keyed_member, monkeypatch
    ):
        import kiro_crew.dashboard.handlers.agents as handlers
        from kiro_crew.memory_stores import UnknownMemoryStore

        async with TestClient(TestServer(self._app(tmp_path))) as client:
            pin, old_path = await self._wear_picture(client, "Release Writer")

            def _refuse_write(*args, **kwargs):
                raise UnknownMemoryStore("config unwritable")

            with monkeypatch.context() as patched:
                patched.setattr(handlers, "persist_member_config", _refuse_write)
                resp = await client.put(
                    "/api/agents/release-writer", json={"display_name": "Release Author"}
                )
                assert resp.status >= 400
            assert old_path.is_file()
            assert KiroCrewConfig.load().agents["release-writer"].display_name == "Release Writer"
            got = await client.get("/api/agents/Release Writer/avatar")
            assert got.status == 200


class TestCreateAndDeleteEdges:
    @pytest.mark.asyncio
    async def test_create_refuses_a_credential_shaped_display_name(self, tmp_path):
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app = _agents_app(tmp_path)
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        cred = "ghp_" + "0123456789abcdefghijABCDEFGHIJ0123456789"
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "alpha", "kiro_agent": "kirocrew", "display_name": cred},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "credential_shaped_name"
        assert "alpha" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_create_refuses_a_handle_that_is_another_members_label(
        self, tmp_path, keyed_member
    ):
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app = _agents_app(tmp_path)
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            for body in (
                {"name": "Release Writer", "kiro_agent": "kirocrew"},
                {
                    "name": "someone-else",
                    "kiro_agent": "kirocrew",
                    "display_name": "Release Writer",
                },
                {"name": "release-writer", "kiro_agent": "kirocrew", "display_name": "Other"},
            ):
                resp = await client.post("/api/agents", json=body)
                assert resp.status == 409, body
                assert (await resp.json())["code"] == "agent_exists"
        assert set(KiroCrewConfig.load().agents) >= {"release-writer"}
        assert "someone-else" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_create_never_mints_a_key_that_is_a_live_label(self, tmp_path, keyed_member):
        # End to end: ``release-writer`` is renamed to the label ``writer``;
        # creating ``Writer!`` slugs to ``writer`` and must NOT take that key,
        # or every request addressed to ``writer`` would route to it.
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app = _agents_app(tmp_path)
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/agents/release-writer", json={"display_name": "writer"})
            assert resp.status == 200
            resp = await client.post(
                "/api/agents", json={"name": "Writer!", "kiro_agent": "kirocrew"}
            )
            assert resp.status in (200, 201), await resp.text()
            created = await resp.json()
        cfg = KiroCrewConfig.load()
        new_key = created.get("member_id") or next(
            key for key, member in cfg.agents.items() if member.display_name == "Writer!"
        )
        assert new_key != "writer" and new_key.startswith("writer-")
        assert members_mod.resolve_member_id("writer", cfg) == "release-writer"
        assert members_mod.resolve_member_id("Writer!", cfg) == new_key

    @pytest.mark.asyncio
    async def test_delete_reaps_a_private_copy_still_owned_by_label(
        self, tmp_path, keyed_member, monkeypatch
    ):
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers import agents as handlers

        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "rw-copy.json").write_text(
            json.dumps({"name": "rw-copy", "prompt": "mine"}), encoding="utf-8"
        )
        monkeypatch.setattr(handlers, "kiro_agents_dir_path", lambda: specs)
        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].kiro_agent = "rw-copy"
        cfg.save()
        # A sidecar the load could not repair names the owner by label.
        agent_state.set_fork_info("rw-copy", forked_from="kirocrew", private_to="Release Writer")
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            assert (await client.delete("/api/agents/Release Writer")).status == 200
        assert not (specs / "rw-copy.json").exists()
        assert agent_state.get_fork_info("rw-copy") is None


class TestLabelledMemberSurfaces:
    """Surfaces that index by the display name must index by the key instead."""

    @pytest.mark.asyncio
    async def test_members_row_reports_its_binding_for_a_labelled_crew(
        self, tmp_path, keyed_member
    ):
        slug = members_mod.member_slug("release-writer")
        members_mod.write_dm_binding(
            slug,
            member="release-writer",
            slot_key=members_mod.member_slot_key(slug, keyed_member.memory_store),
            memory_store=keyed_member.memory_store,
        )
        async with TestClient(TestServer(_agents_app(tmp_path))) as client:
            body = await (await client.get("/api/members")).json()
        row = next(m for m in body["members"] if m["member_id"] == "release-writer")
        assert row["name"] == "Release Writer"
        assert row["slot_key"] == members_mod.member_slot_key(slug, keyed_member.memory_store)

    def test_member_identity_text_uses_the_display_name(self, keyed_member):
        from kiro_crew.context import ContextBuilder

        builder = ContextBuilder.__new__(ContextBuilder)
        text = ContextBuilder._build_member_section(
            builder, "release-writer", strict=True, include_briefing=False
        )
        assert "You are Release Writer." in text
        assert "You are release-writer" not in text

    def test_create_purges_a_team_entry_left_under_the_label(self, keyed_member):
        from kiro_crew import crew_teams
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        crew_teams.write_teams(
            [crew_teams.Team(id="aaaaaaaaaaaa", name="Docs", members=["Release Writer"])]
        )
        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer"].display_name = "Release Author"
        cfg.save()
        cfg = KiroCrewConfig.load()
        cfg.agents["release-writer-2"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        provision_member_memory(cfg, "release-writer-2")
        persist_member_config(cfg, "release-writer-2", create=True)
        # The stale label entry was purged with the create, so canonicalizing the
        # document onto the new member yields no membership.
        stored = crew_teams.read_teams()[0].members
        assert stored == []

    def test_capabilities_see_an_overlay_entry_filed_under_the_legacy_key(self):
        from kiro_crew.agent_capabilities import _canonical_overlay_agents

        effective = {
            "agents": {
                "writer": {"member_id": "writer", "display_name": "Writer", "kiro_agent": "k"}
            }
        }
        overlay = {"agents": {"Writer": {"kiro_agent": "k-local"}}}
        assert _canonical_overlay_agents(effective, overlay) == {
            "writer": {"kiro_agent": "k-local"}
        }

    def test_labelled_members_store_is_recognised_as_owned(self, keyed_member):
        # The record's ``owner_member`` is the label ("Release Writer"); the
        # readers compare against the KEY. ``owner_member_id`` decides.
        from kiro_crew.dashboard.chat_persistence import member_store_ownership_holds
        from kiro_crew.dashboard.session_control import _store_is_member_owned
        from kiro_crew.memory_stores import store_owned_by_member

        cfg = KiroCrewConfig.load()
        store = cfg.agents["release-writer"].memory_store
        record = cfg.memory_stores[store]
        assert record.owner_member == "Release Writer"
        assert store_owned_by_member(record, "release-writer", cfg)
        assert member_store_ownership_holds(cfg, "release-writer", store)
        assert _store_is_member_owned(store)
        # A member that later takes the label owns nothing of it.
        cfg.agents["release-writer"].display_name = "Author"
        cfg.agents["other"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="other", display_name="Release Writer"
        )
        assert not store_owned_by_member(record, "other", cfg)
        assert store_owned_by_member(record, "release-writer", cfg)

    def test_legacy_owner_label_resolves_through_the_member_it_names(self):
        from kiro_crew.memory_stores import store_owned_by_member

        cfg = KiroCrewConfig.load()
        cfg.agents["dr-eggbot"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="dr-eggbot", display_name="Dr. Eggbot"
        )
        legacy = loader_module.MemoryStoreConfig(owner_member="Dr. Eggbot", memory_version=2)
        assert store_owned_by_member(legacy, "dr-eggbot", cfg)
        assert not store_owned_by_member(legacy, "someone", cfg)
        # A shared label answers nobody: refused, never guessed.
        cfg.agents["twin"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", member_id="twin", display_name="Dr. Eggbot"
        )
        assert not store_owned_by_member(legacy, "dr-eggbot", cfg)

    def test_picture_of_a_labelled_member_that_keeps_its_key_moves_onto_the_label(self):
        # ``researcher`` is already keyed by its id, so the plan never moves it;
        # its picture, filed under the key by the previous build, must still
        # land under the label the avatar route reads.
        from kiro_crew.members import avatar_stem, avatars_root

        data = _old_shape()
        data["agents"] = {
            "default": data["agents"]["default"],
            "researcher": {
                "member_id": "researcher",
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "display_name": "Research Lead",
            },
        }
        data["default_agent"] = "default"
        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{avatar_stem('researcher')}.0123456789abcdef.png").write_bytes(b"png")
        path = _write_config(data)
        before = path.read_text(encoding="utf-8")
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        assert (root / f"{avatar_stem('Research Lead')}.0123456789abcdef.png").is_file()
        assert not (root / f"{avatar_stem('researcher')}.0123456789abcdef.png").exists()
        # Nothing moved in the document, so it is not rewritten.
        assert path.read_text(encoding="utf-8") == before

    def test_rekey_moves_a_picture_filed_under_the_old_key_onto_the_label(self):
        from kiro_crew.members import avatar_stem, avatars_root

        data = _old_shape()
        root = avatars_root()
        root.mkdir(parents=True, exist_ok=True)
        # ``Dr. Eggbot`` already carried the explicit label ``Doctor Eggbot``;
        # its picture was filed under the key it was stored by.
        (root / f"{avatar_stem('Dr. Eggbot')}.abcdef0123456789.png").write_bytes(b"png")
        # ``Crew Program Manager`` takes its old key as label: nothing to move.
        (root / f"{avatar_stem('Crew Program Manager')}.0123456789abcdef.png").write_bytes(b"png")
        _write_config(data)
        loader_module.migrate_member_identity()
        KiroCrewConfig.load()
        assert (root / f"{avatar_stem('Doctor Eggbot')}.abcdef0123456789.png").is_file()
        assert not (root / f"{avatar_stem('Dr. Eggbot')}.abcdef0123456789.png").exists()
        assert (root / f"{avatar_stem('Crew Program Manager')}.0123456789abcdef.png").is_file()


class TestIdentityNamespaceProperty:
    """One namespace, four invariants, under any sequence of member operations.

    The identity namespace is {``config.agents`` keys, ``display_name`` labels,
    legacy pre-migration keys, allocated ids, retired store owners}. The
    handlers' guards (``resolve_member`` on create and rename, the allocator's
    reservation set, the migration planner's survivor rule) exist to keep it
    consistent; this test drives them with random labels drawn from a small
    alphabet built to collide (``writer`` / ``Writer`` / ``Writer!`` all slug to
    ``writer``) and checks, after every step:

    (a) no two live members share a key -- and a document round-trip through
        the migration keeps every entry;
    (b) no live member's ``display_name`` equals another live member's key;
    (c) every live handle (key or label) resolves to exactly its member;
    (d) writing a legacy rendering of the state (some entries filed under their
        label) and migrating it recovers the state, and migrating again moves
        nothing.
    """

    LABELS = ["writer", "Writer", "Writer!", "author", "Author", "x y", "x-y"]

    @staticmethod
    def _model_create(cfg, label: str) -> str | None:
        # Mirrors ``create_agent``: refused when either handle names a member.
        from kiro_crew.memory_stores import _allocate_member_id

        if members_mod.resolve_member(label, cfg) is not None:
            return None
        key = _allocate_member_id(cfg, label)
        cfg.agents[key] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name=label, member_id=key, memory_store=f"store-{key}"
        )
        cfg.memory_stores[f"store-{key}"] = loader_module.MemoryStoreConfig(
            owner_member_id=key, owner_member=label, memory_version=2
        )
        return key

    @staticmethod
    def _model_rename(cfg, handle: str, new_label: str) -> None:
        # Mirrors ``update_agent``: refused when the label names another member.
        hit = members_mod.resolve_member(handle, cfg)
        if hit is None:
            return
        taken = members_mod.resolve_member(new_label, cfg)
        if taken is not None and taken[0] != hit[0]:
            return
        hit[1].display_name = new_label

    @staticmethod
    def _model_delete(cfg, handle: str) -> None:
        # Mirrors ``delete_agent``: the record goes, the retired store stays.
        hit = members_mod.resolve_member(handle, cfg)
        if hit is not None:
            del cfg.agents[hit[0]]

    @staticmethod
    def _live_handles(cfg) -> list[str]:
        handles = list(cfg.agents)
        handles.extend(member.display_name for member in cfg.agents.values() if member.display_name)
        # Insertion order, not sorted: a collision-suffixed id is random, so a
        # sort would order the handles differently on hypothesis' replay.
        return list(dict.fromkeys(handles))

    @classmethod
    def _check(cls, cfg) -> None:
        keys = set(cfg.agents)
        for key, member in cfg.agents.items():
            label = member.display_name
            # (b)
            assert not label or label == key or label not in keys, (key, label, keys)
            # (c)
            assert members_mod.resolve_member_id(key, cfg) == key
            if label:
                assert members_mod.resolve_member_id(label, cfg) == key, (label, key)
            # every id equals its key from the first write
            assert member.member_id == key

    @classmethod
    def _document(cls, cfg, legacy: list[bool]) -> dict:
        doc = {}
        for (key, member), as_legacy in zip(cfg.agents.items(), legacy):
            entry = {
                "member_id": key,
                "kiro_agent": "kirocrew",
                "display_name": member.display_name,
            }
            doc[member.display_name if as_legacy and member.display_name else key] = entry
        return doc

    @given(data=st.data())
    @settings(max_examples=300, deadline=None)
    def test_namespace_stays_consistent(self, data):
        cfg = KiroCrewConfig()
        cfg.agents = {}
        cfg.memory_stores = {}
        labels = st.sampled_from(self.LABELS)
        for _ in range(data.draw(st.integers(min_value=0, max_value=10), label="steps")):
            live = self._live_handles(cfg)
            op = data.draw(st.sampled_from(["create", "rename", "delete"] if live else ["create"]))
            if op == "create":
                self._model_create(cfg, data.draw(labels, label="create"))
            elif op == "rename":
                self._model_rename(
                    cfg,
                    data.draw(st.sampled_from(live), label="rename"),
                    data.draw(labels, label="to"),
                )
            else:
                self._model_delete(cfg, data.draw(st.sampled_from(live), label="delete"))
            self._check(cfg)  # (a) is the dict itself; (b) and (c) inside
        # (d) a legacy rendering migrates back to exactly this state, once.
        legacy = data.draw(
            st.lists(st.booleans(), min_size=len(cfg.agents), max_size=len(cfg.agents))
        )
        doc = self._document(cfg, legacy)
        rekeyed, plan = loader_module.rekey_agents_document(doc)
        assert len(rekeyed) == len(cfg.agents) == len(doc)
        assert set(rekeyed) == set(cfg.agents)
        for key, entry in rekeyed.items():
            assert entry["display_name"] == cfg.agents[key].display_name
        again, plan_again = loader_module.rekey_agents_document(dict(rekeyed))
        assert plan_again == {} and again == rekeyed

    @given(
        entries=st.lists(
            st.tuples(
                st.sampled_from(["a", "b", "c", "d", "A", "B"]),
                st.sampled_from(["", "a", "b", "c", "d", "shared"]),
            ),
            max_size=6,
            unique_by=lambda e: e[0],
        )
    )
    @settings(max_examples=300, deadline=None)
    def test_arbitrary_documents_migrate_without_loss_and_idempotently(self, entries):
        # Any document, however tangled (duplicate ids, chains, cycles): every
        # entry survives the rewrite and a second rewrite is a no-op.
        doc = {key: {"member_id": member_id} for key, member_id in entries}
        rekeyed, plan = loader_module.rekey_agents_document(dict(doc))
        assert len(rekeyed) == len(doc)
        assert sorted(e["member_id"] for e in rekeyed.values()) == sorted(
            e["member_id"] for e in doc.values()
        )
        for old_key, new_key in plan.items():
            assert doc[old_key]["member_id"] == new_key
        again, plan_again = loader_module.rekey_agents_document(dict(rekeyed))
        assert plan_again == {} and again == rekeyed
