"""Compatibility contract of the :mod:`kiro_crew.onboarding_import` facade.

The engine is composed of cohesive owners (``onboarding_scan``,
``onboarding_plan``, ``onboarding_sources``, ``onboarding_apply``) behind the
``onboarding_import`` facade. These tests pin what the facade promises to the
code that imports it -- the dashboard handler, ``mcp_cleanup``, the managed-MCP
registration tests and the persistence ratchets -- independently of where each
rule is implemented:

* the importable names and the public signatures;
* that every re-exported name IS the canonical owner's object, so a caller of
  the facade and a caller of the owner can never observe two implementations;
* that every engine warning is still emitted on the ``kiro_crew.onboarding_import``
  logger, which operators and tests filter on;
* that the deferred imports stay deferred, so importing the facade does not pull
  the dashboard MCP handler, MCP discovery or the cron service into a process;
* that every owner module imports cleanly on its own, in any order.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import onboarding_import
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.interfaces import ImportSource

_FACADE_LOGGER = "kiro_crew.onboarding_import"

#: Names reached through the facade by code this engine does not own -- imported,
#: or cited by their ``onboarding_import.<name>`` path in specs, docstrings and
#: comments that must keep resolving. The list is a contract, not an inventory.
_CONSUMER_NAMES = (
    # Public API: the dashboard handler (``_backend()``) and the specs.
    "detect_sources",
    "preview_import",
    "apply_import",
    "CATEGORY_IDS",
    "CONFLICT_STRATEGIES",
    "STRATEGY_SKIP",
    "STRATEGY_RENAME",
    "STRATEGY_OVERWRITE",
    "STRATEGY_CATEGORIES",
    # mcp_cleanup's deferred imports.
    "predecessor_mcp_names",
    "stale_mcp_binaries",
    # Imported by the managed-MCP registration, lesson-outcome and frontmatter tests.
    "_managed_mcp_names",
    "_Item",
    "_write_instruction",
    "_frontmatter",
    "_column0_activation_declared",
    # Located by ``onboarding_import.py::<name>`` in the persistence and cron ratchets.
    "_write_memory",
    "_write_schedule",
    # Cited by facade path: platform-context.md / interfaces.py (``_sources()``),
    # the handler's ``_SOURCE_ID_SHAPE_RE`` note, test_yaml_safe_loading's docstring
    # (``_load_no_alias_yaml``), the managed-name and writer-outcome vocabulary.
    "_CORE_MANAGED_MCP_NAMES",
    "_WriteOutcome",
    "_load_no_alias_yaml",
    "_sources",
    "_Source",
    "_SOURCE_ID_RE",
    "_scan_source",
    "_preview",
)

#: Consumer names the facade DEFINES rather than re-exports.
_FACADE_DEFINED = (
    "detect_sources",
    "preview_import",
    "apply_import",
    "_write_instruction",
    "_write_memory",
    "_write_schedule",
    "_preview",
)

#: The canonical owner of every re-exported consumer name.
_OWNERS = {
    "CATEGORY_IDS": "kiro_crew.onboarding_scan",
    "_Item": "kiro_crew.onboarding_scan",
    "_frontmatter": "kiro_crew.onboarding_scan",
    "_column0_activation_declared": "kiro_crew.onboarding_scan",
    "_load_no_alias_yaml": "kiro_crew.onboarding_scan",
    "CONFLICT_STRATEGIES": "kiro_crew.onboarding_apply",
    "STRATEGY_SKIP": "kiro_crew.onboarding_apply",
    "STRATEGY_RENAME": "kiro_crew.onboarding_apply",
    "STRATEGY_OVERWRITE": "kiro_crew.onboarding_apply",
    "STRATEGY_CATEGORIES": "kiro_crew.onboarding_apply",
    "_WriteOutcome": "kiro_crew.onboarding_apply",
    "predecessor_mcp_names": "kiro_crew.onboarding_sources",
    "stale_mcp_binaries": "kiro_crew.onboarding_sources",
    "_managed_mcp_names": "kiro_crew.onboarding_sources",
    "_sources": "kiro_crew.onboarding_sources",
    "_Source": "kiro_crew.onboarding_sources",
    "_SOURCE_ID_RE": "kiro_crew.onboarding_sources",
    "_CORE_MANAGED_MCP_NAMES": "kiro_crew.onboarding_sources",
    "_scan_source": "kiro_crew.onboarding_sources",
}

#: Every module of the engine, facade included.
_ENGINE_MODULES = (
    "kiro_crew.onboarding_import",
    "kiro_crew.onboarding_scan",
    "kiro_crew.onboarding_plan",
    "kiro_crew.onboarding_apply",
    "kiro_crew.onboarding_sources",
    "kiro_crew.onboarding_sources.claude_code",
    "kiro_crew.onboarding_sources.codex",
    "kiro_crew.onboarding_sources.gemini",
    "kiro_crew.onboarding_sources.hermes",
    "kiro_crew.onboarding_sources.lineage",
    "kiro_crew.onboarding_sources.openclaw",
)

#: Modules the facade must NOT load at import time. Each is imported lazily at
#: the one call site that needs it (the MCP sidecar lock, the alias census, the
#: cron service), because loading them eagerly either inverts an import order
#: (the dashboard handler imports this engine during gateway startup) or adds
#: boot weight every importer pays.
_DEFERRED_MODULES = (
    "kiro_crew.dashboard.handlers.mcp",
    "kiro_crew.mcp_discovery",
    "kiro_crew.cron",
)


def _lineage_source(**overrides) -> ImportSource:
    fields: dict = {
        "id": "predecessor",
        "display_name": "Predecessor",
        "env_vars": ("PREDECESSOR_HOME",),
        "home_dir": ".predecessor",
    }
    fields.update(overrides)
    return ImportSource(**fields)


@pytest.fixture
def install_sources():
    """Compose a context whose edition contributes the given sources."""

    def _install(*sources: ImportSource) -> None:
        class _Provider:
            def import_sources(self) -> list[ImportSource]:
                return list(sources)

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, import_sources=_Provider()))

    yield _install
    reset_context()


class TestFacadeSurface:
    @pytest.mark.parametrize("name", _CONSUMER_NAMES)
    def test_every_consumer_name_is_importable(self, name: str) -> None:
        assert hasattr(onboarding_import, name), f"onboarding_import.{name} is gone"

    def test_public_signatures_are_unchanged(self) -> None:
        assert str(inspect.signature(onboarding_import.detect_sources)) == (
            "(home: 'Path | None' = None, env: 'Mapping[str, str] | None' = None)"
            " -> 'dict[str, Any]'"
        )
        assert str(inspect.signature(onboarding_import.preview_import)) == (
            "(source_ids: 'list[str] | None' = None, home: 'Path | None' = None, "
            "env: 'Mapping[str, str] | None' = None) -> 'dict[str, Any]'"
        )
        assert str(inspect.signature(onboarding_import.apply_import)) == (
            "(plan: 'dict[str, Any]', *, data_home: 'Path | None' = None, "
            "cron_service: 'Any' = None, vector_store: 'VectorMemoryStore | None' = None, "
            "lesson_store: 'Any' = None, conflict_strategy: 'str' = 'skip') -> 'dict[str, Any]'"
        )

    def test_the_facade_logger_keeps_its_name(self) -> None:
        assert onboarding_import.logger.name == _FACADE_LOGGER

    @pytest.mark.parametrize("name", sorted(set(_CONSUMER_NAMES) - set(_FACADE_DEFINED)))
    def test_a_re_exported_name_is_its_owners_object(self, name: str) -> None:
        owner = importlib.import_module(_OWNERS[name])
        assert getattr(onboarding_import, name) is getattr(owner, name)

    @pytest.mark.parametrize("name", _FACADE_DEFINED)
    def test_a_facade_defined_name_is_defined_here(self, name: str) -> None:
        # The persistence-switch and cron-probe ratchets locate these writers by
        # ``onboarding_import.py::<name>``; the plan/apply entry points are here too.
        assert getattr(onboarding_import, name).__module__ == "kiro_crew.onboarding_import"


class TestLoggerIdentity:
    """Every engine warning lands on the facade's logger, whichever owner emits it."""

    @staticmethod
    def _engine_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if record.name.startswith("kiro_crew.onboarding") and record.levelno >= logging.WARNING
        ]

    def test_registry_warnings(self, caplog, install_sources) -> None:
        install_sources(_lineage_source(id="../escape"))
        with caplog.at_level(logging.WARNING):
            onboarding_import._sources()
        records = self._engine_records(caplog)
        assert any("becomes a path segment" in record.getMessage() for record in records)
        assert {record.name for record in records} == {_FACADE_LOGGER}

    def test_scanner_failure_warning(self, caplog, tmp_path: Path) -> None:
        def _explode(scan) -> None:
            raise RuntimeError("reader died")

        source = onboarding_import._Source(
            id="predecessor",
            display_name="Predecessor",
            scan=_explode,
            env_vars=(),
            home_dir=".predecessor",
            managed_mcp_names=frozenset(),
            superseded=False,
            stale_mcp_binaries=frozenset(),
        )
        root = tmp_path / ".predecessor"
        root.mkdir()
        with caplog.at_level(logging.WARNING):
            onboarding_import._scan_source("predecessor", root, tmp_path, source=source)
        records = self._engine_records(caplog)
        assert any("import scanner" in record.getMessage() for record in records)
        assert {record.name for record in records} == {_FACADE_LOGGER}

    def test_writer_warnings(self, caplog, tmp_path: Path, monkeypatch) -> None:
        """Restore-copy failures (MCP and skill) and a failed write, end to end."""
        # The MCP writer takes the dashboard's sidecar lock, which resolves under
        # the real ~/.kiro unless redirected: the host floor only rebinds it when
        # the handler module was already imported, which is not yet the case when
        # this test is the first one a process runs.
        mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
        global_mcp = tmp_path / "kiro" / "settings" / "mcp.json"
        monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", global_mcp)
        monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", global_mcp.with_suffix(".lock"))
        home = tmp_path / "home"
        codex = home / ".codex"
        (codex / "skills" / "demo").mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        project_toml = str(project).replace("\\", "\\\\")

        def _write_source(command: str, body: str) -> None:
            (codex / "config.toml").write_text(
                f'[mcp_servers.helper]\ncommand = "{command}"\n'
                f'[projects."{project_toml}"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            (codex / "skills" / "demo" / "SKILL.md").write_text(
                f"---\nname: demo\n---\n{body}\n", encoding="utf-8"
            )

        destination = tmp_path / "destination"
        destination.mkdir()
        # An unreadable destination config makes the workspace write fail.
        (destination / "config.json").write_text("{not json", encoding="utf-8")
        _write_source("first-helper", "First body.")
        with caplog.at_level(logging.WARNING):
            first = onboarding_import.apply_import(
                onboarding_import.preview_import(home=home, env={}), data_home=destination
            )
        assert any(entry["reason"] == "write_failed" for entry in first["skipped"])

        # Change both definitions upstream, then block the restore directory so
        # the pre-overwrite copy cannot be written: the overwrite must refuse.
        _write_source("second-helper", "Second body.")
        (destination / "imports" / "replaced").write_text("not a directory", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            second = onboarding_import.apply_import(
                onboarding_import.preview_import(home=home, env={}),
                data_home=destination,
                conflict_strategy="overwrite",
            )
        assert {entry["category_id"] for entry in second["conflicts"]} >= {
            "mcp_servers",
            "skills",
        }

        messages = [record.getMessage() for record in self._engine_records(caplog)]
        assert any("Foreign-agent import failed" in message for message in messages)
        assert any("MCP server being replaced" in message for message in messages)
        assert any("skill being replaced" in message for message in messages)
        assert {record.name for record in self._engine_records(caplog)} == {_FACADE_LOGGER}


class TestManagedNamesDuringAScan:
    """A contributed managed name is excluded while another source is scanned."""

    def test_an_unwired_scan_refuses_to_guess_managed_names(self, tmp_path: Path) -> None:
        """A scan built outside ``_scan_source`` fails closed at the MCP projection."""
        from kiro_crew.onboarding_scan import _Scan

        scan = _Scan(source_id="codex", root=tmp_path, user_home=tmp_path)
        with pytest.raises(RuntimeError, match="no registry"):
            scan.managed_mcp_names()

    def test_the_scan_consults_the_live_registry(self, tmp_path: Path, install_sources):
        """The lookup is the registry's own function, evaluated when the projection asks."""
        install_sources(_lineage_source(managed_mcp_names=("Predecessor-Core",)))
        seen: list = []
        source = dataclasses.replace(
            onboarding_import._sources()["codex"],
            scan=lambda scan: seen.append(scan.managed_mcp_names),
        )
        root = tmp_path / ".codex"
        root.mkdir()
        onboarding_import._scan_source("codex", root, tmp_path, source=source)
        assert seen == [onboarding_import._managed_mcp_names]
        assert "predecessor-core" in seen[0]()

    def test_a_contributed_managed_server_is_not_imported(self, tmp_path: Path, install_sources):
        install_sources(_lineage_source(managed_mcp_names=("Predecessor-Core",)))
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text(
            '[mcp_servers.predecessor-core]\ncommand = "pc"\n'
            '[mcp_servers.kept]\ncommand = "kept"\n',
            encoding="utf-8",
        )

        plan = onboarding_import.preview_import(["codex"], home=home, env={})

        codex_plan = next(source for source in plan["sources"] if source["id"] == "codex")
        counts = {category["id"]: category["count"] for category in codex_plan["categories"]}
        assert counts["mcp_servers"] == 1
        assert any(
            entry["source_id"] == "codex" and entry["reason"] == "managed_server_excluded"
            for entry in plan["skipped"]
        )


def _run_python(tmp_path: Path, code: str) -> str:
    env_home = tmp_path / "kc-home"
    env_home.mkdir(exist_ok=True)
    # The whole environment is inherited, because Windows needs SYSTEMROOT and
    # friends to start the interpreter at all; only the home and data-home
    # variables are pinned under tmp_path. The child runs sys.executable by path
    # and spawns nothing, so the inherited PATH cannot reach a version-manager shim.
    env = {
        **os.environ,
        "KIROCREW_HOME": str(env_home),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PYTHONPATH": str(Path(onboarding_import.__file__).resolve().parents[1]),
        "KIROCREW_TELEMETRY": "0",
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


class TestImportTimeBehaviour:
    def test_importing_the_facade_keeps_the_deferred_imports_deferred(self, tmp_path) -> None:
        out = _run_python(
            tmp_path,
            "import json, sys\n"
            "import kiro_crew.onboarding_import\n"
            f"print(json.dumps([m for m in {list(_DEFERRED_MODULES)!r} if m in sys.modules]))\n",
        )
        assert json.loads(out.strip().splitlines()[-1]) == []

    @pytest.mark.parametrize("module", _ENGINE_MODULES)
    def test_every_engine_module_imports_first(self, tmp_path, module: str) -> None:
        """No import cycle: any module can be the process's first engine import."""
        out = _run_python(
            tmp_path,
            f"import {module}\n"
            "import kiro_crew.onboarding_import as facade\n"
            "print(facade.preview_import.__module__)\n",
        )
        assert out.strip().splitlines()[-1] == "kiro_crew.onboarding_import"

    def test_importing_mcp_cleanup_does_not_load_the_engine(self, tmp_path) -> None:
        out = _run_python(
            tmp_path,
            "import sys\n"
            "import kiro_crew.mcp_cleanup\n"
            "print('kiro_crew.onboarding_import' in sys.modules)\n",
        )
        assert out.strip().splitlines()[-1] == "False"


def test_the_claude_code_source_id_is_the_foreign_app_not_the_provider() -> None:
    """Every engine module keeps ``"claude_code"`` as a source id, never a provider check.

    ``test_agent_sdk_provider_identity`` reads only ``onboarding_import.py``; the
    Claude Code descriptor and adapter now live in ``onboarding_sources``, so the
    same rule is held over the whole engine here.
    """
    src = Path(onboarding_import.__file__).resolve().parent
    engine = [src / f"{name.split('.', 1)[1].replace('.', '/')}.py" for name in _ENGINE_MODULES]
    engine = [path if path.is_file() else path.with_suffix("") / "__init__.py" for path in engine]
    for path in engine:
        assert "is_claude_code" not in path.read_text(encoding="utf-8"), path
    assert "claude_code" in onboarding_import._sources()


def test_the_facade_module_is_importable_through_importlib() -> None:
    assert importlib.import_module("kiro_crew.onboarding_import") is onboarding_import
