"""Conservative import of user-owned data from other local agent tools.

This module is the engine's entry point and compatibility facade (see
docs/system-specs/modules/onboarding-import.md). It composes the owners below
and keeps the pieces that must answer for the whole run:

* ``detect_sources`` / ``preview_import`` -- one registry snapshot, the
  discovery of which sources are installed, and the plan document assembled
  from each installed source's scan;
* ``apply_import`` -- the rescan, the per-item dispatch loop, the ledger flush
  discipline, and the result the dashboard reports;
* the writers into Kiro Crew's own stores: lessons (``_write_instruction``),
  memories (``_write_memory``) and schedules (``_write_schedule``).

Owners, bottom up: :mod:`kiro_crew.onboarding_scan` (safe reads and content
screens), :mod:`kiro_crew.onboarding_plan` (normalized items and the plan
document), :mod:`kiro_crew.onboarding_sources` (the source registry and one
adapter per foreign layout), and :mod:`kiro_crew.onboarding_apply` (ledger,
conflict strategies and the file-backed writers). The names other modules
import from here are re-exported from those owners unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import config_dir
from kiro_crew.embeddings import make_sync_embed_fn
from kiro_crew.learn import _MAX_LESSONS_TOTAL, Lesson, LessonStore
from kiro_crew.lesson_validation import contains_volatile_lesson_fact
from kiro_crew.onboarding_apply import CONFLICT_STRATEGIES  # noqa: F401 - facade surface
from kiro_crew.onboarding_apply import STRATEGY_OVERWRITE  # noqa: F401 - facade surface
from kiro_crew.onboarding_apply import STRATEGY_RENAME  # noqa: F401 - facade surface
from kiro_crew.onboarding_apply import (
    _LEDGER_RELATIVE_PATH,
    _REPLACEABLE_CATEGORIES,
    STRATEGY_CATEGORIES,
    STRATEGY_SKIP,
    _load_ledger,
    _normalize_strategy,
    _record_ledger,
    _write_json,
    _write_mcp,
    _write_settings,
    _write_skill,
    _write_workspace,
    _WriteOutcome,
)
from kiro_crew.onboarding_plan import (
    _plan_from_scans,
    _plan_private_paths,
    _plan_roots,
    _plan_user_homes,
    _selected_pairs,
)
from kiro_crew.onboarding_scan import _column0_activation_declared  # noqa: F401 - facade surface
from kiro_crew.onboarding_scan import _frontmatter  # noqa: F401 - facade surface
from kiro_crew.onboarding_scan import _load_no_alias_yaml  # noqa: F401 - facade surface
from kiro_crew.onboarding_scan import CATEGORY_IDS, _is_link_like, _Item, _Scan, _stat_kind
from kiro_crew.onboarding_sources import _CORE_MANAGED_MCP_NAMES  # noqa: F401 - facade surface
from kiro_crew.onboarding_sources import _SOURCE_ID_RE  # noqa: F401 - facade surface
from kiro_crew.onboarding_sources import _managed_mcp_names  # noqa: F401 - facade surface
from kiro_crew.onboarding_sources import _Source  # noqa: F401 - facade surface
from kiro_crew.onboarding_sources import predecessor_mcp_names  # noqa: F401 - facade surface
from kiro_crew.onboarding_sources import stale_mcp_binaries  # noqa: F401 - facade surface
from kiro_crew.onboarding_sources import _scan_source, _source_context, _source_roots, _sources
from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)


def _source_exists(source_id: str, root: Path) -> bool:
    if _is_link_like(root):
        return False
    if _stat_kind(root) == "dir":
        return True
    if source_id == "claude_code":
        global_config = root.parent / ".claude.json"
        return _stat_kind(global_config) == "file" and not _is_link_like(global_config)
    return False


def _preview(
    source_ids: list[str] | None,
    home: Path | None,
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    # ONE registry snapshot for the whole preview: id validation, root resolution,
    # scanner dispatch and the reported display name must all agree, and each
    # re-read is a fail-closed context call that can independently degrade to the
    # builtins.
    registry = _sources()
    known = tuple(registry)
    requested = list(known) if source_ids is None else list(dict.fromkeys(source_ids))
    unknown = [source_id for source_id in requested if source_id not in known]
    requested = [source_id for source_id in requested if source_id in known]
    base_home, roots = _source_roots(home, env, sources=registry)
    env_map = os.environ if env is None else env
    scans = []
    for source_id in requested:
        root = roots.get(source_id)
        if root is None:
            # Locatable only through env vars, none of which are set.
            continue
        config_paths, workspace_paths = _source_context(source_id, root, base_home, env_map)
        if (
            _source_exists(source_id, root)
            or _is_link_like(root)
            or any(path.is_file() for path in config_paths)
        ):
            scans.append(
                _scan_source(
                    source_id,
                    root,
                    base_home,
                    config_paths=config_paths,
                    workspace_paths=workspace_paths,
                    source=registry[source_id],
                )
            )
    return _plan_from_scans(
        scans,
        unknown,
        {source_id: source.display_name for source_id, source in registry.items()},
    )


def detect_sources(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Detect supported foreign-agent homes and summarize importable categories."""
    return _preview(None, home, env)


def preview_import(
    source_ids: list[str] | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a content-free, selectable import plan."""
    return _preview(source_ids, home, env)


def _lessons_overlap(incoming: str, existing: str) -> bool:
    """Whether two lesson rules are close enough to treat as the same lesson.

    Tracks ``VectorMemoryStore.write_lesson``'s own dedupe (substring, then
    significant-word overlap) so import RECOGNIZES the same collisions -- but
    reports them instead of replacing, which is what that writer would do.

    The overlap divisor is deliberately the SMALLER word set here, which is
    stricter than the writer's (that one divides by the larger set, because a
    false positive there DELETES the stored lesson). Import is merge-only, so a
    false positive costs at most a skipped foreign directive that the user can
    still teach by hand -- the conservative direction for a boundary that
    ingests another agent's instructions.
    """

    left = incoming.lower().strip()
    right = existing.lower().strip()
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    left_words = {word for word in re.findall(r"[a-z0-9]{4,}", left)}
    right_words = {word for word in re.findall(r"[a-z0-9]{4,}", right)}
    if not left_words or not right_words:
        return False
    shared = left_words & right_words
    return len(shared) / min(len(left_words), len(right_words)) > 0.5


def _write_instruction(
    item: _Item,
    lesson_store: Any,
    vector_store: VectorMemoryStore | None = None,
) -> _WriteOutcome:
    """Append one imported directive to the highest-priority durable tier.

    ``LessonStore.save`` is itself exact-rule deduplicating, so a re-import is
    naturally idempotent; this reports ``existing`` for that case so the ledger
    still records it and the outcome is ``deduplicated`` rather than a false
    ``accepted``.
    """

    rule = str(item.payload.get("rule", "")).strip()
    if not rule:
        return _WriteOutcome("rejected")
    if contains_volatile_lesson_fact(rule):
        return _WriteOutcome("rejected")

    # ContextBuilder reads lesson.* from the VECTOR store when it holds any, and
    # then never reads lessons.jsonl (context.py: `if memory.vector_store and
    # memory.vector_store.get_lessons()`). Writing only the JSONL there would
    # record the item as imported while the agent never sees it, so route through
    # whichever store is actually authoritative.
    # Route on AVAILABILITY, not current emptiness. An empty vector store still
    # becomes authoritative the moment any native lesson lands, and ContextBuilder
    # then stops reading lessons.jsonl -- so a JSONL write made while the store
    # happened to be empty would silently disappear later, with the ledger
    # preventing a re-import.
    if vector_store is not None:
        # NOT ``write_lesson``: it deletes an existing lesson on exact-substring
        # OR >50% topic overlap ("newer replaces older"), which for an import
        # means a foreign directive can delete a correction the USER taught the
        # agent. Import is merge-only, so overlap yields ``existing`` (nothing
        # written, nothing deleted) and only a genuinely new rule is inserted --
        # via the absent-only writer, which cannot replace anything.
        for existing in vector_store.get_lessons():
            try:
                stored = json.loads(str(existing.get("value_json", "")))
            except (TypeError, ValueError):
                continue
            stored_rule = str(stored.get("rule", "")) if isinstance(stored, dict) else str(stored)
            if _lessons_overlap(rule, stored_rule):
                return _WriteOutcome("existing")
        key = f"lesson.{hashlib.sha256(rule.encode()).hexdigest()[:16]}"
        outcome = vector_store.set_semantic_if_absent(
            key,
            {"rule": rule, "category": "preference", "negative": None},
            1.0,
            "import",
        )
        return _WriteOutcome("imported" if outcome == "imported" else "existing")

    if lesson_store is None:
        return _WriteOutcome("rejected")
    load_all = getattr(lesson_store, "load_all", None)
    if callable(load_all):
        existing_lessons = list(load_all())
        normalized = rule.lower()
        for existing in existing_lessons:
            if str(getattr(existing, "rule", "")).lower().strip() == normalized:
                return _WriteOutcome("existing")
        # ``LessonStore.save`` prunes OLDEST-first once the store passes its own
        # ceiling, and the user's own corrections are the oldest entries. A
        # per-import cap alone does not protect them: 151 existing + 50 imported
        # still evicts one. Refuse to write past the store's REMAINING capacity
        # so an import can never delete a lesson the user taught the agent.
        if len(existing_lessons) >= _MAX_LESSONS_TOTAL:
            return _WriteOutcome("rejected")
    outcome = lesson_store.save(
        Lesson(
            ts=datetime.now(timezone.utc).isoformat(),
            rule=rule,
            category="preference",
        )
    )
    return _WriteOutcome("rejected" if outcome == "refused" else "imported")


def _write_memory(
    item: _Item,
    data_home: Path,
    vector_store: VectorMemoryStore | None,
) -> _WriteOutcome:
    if isinstance(item.payload, dict) and item.payload.get("kind") == "semantic":
        if vector_store is None:
            return _WriteOutcome("rejected")
        key = str(item.payload["key"])
        value = item.payload["value"]
        outcome = vector_store.set_semantic_if_absent(
            key,
            value,
            float(item.payload["confidence"]),
            "import",
        )
        if outcome == "imported":
            return _WriteOutcome("imported")
        existing = vector_store.get_semantic(key)
        if existing is not None:
            try:
                same = json.loads(existing["value_json"]) == value
                return _WriteOutcome("existing" if same else "conflict")
            except (KeyError, TypeError, json.JSONDecodeError, RecursionError):
                return _WriteOutcome("conflict")
        return _WriteOutcome("rejected")
    if isinstance(item.payload, dict) and item.payload.get("kind") == "episodic":
        if vector_store is None:
            return _WriteOutcome("rejected")
        text = str(item.payload["text"])
        if vector_store.has_episodic_text(text):
            return _WriteOutcome("existing")
        # Embed OFF the request. Inference cost grows with text length (~0.4s per
        # 2000-char chunk on CPU), and import writes hundreds of chunks, so an
        # inline embed makes the user watch a spinner for minutes. The row is
        # keyword-searchable immediately and the caller schedules the backfill
        # sweep that fills the vector in (see ``schedule_embedding_backfill``).
        # Batching is NOT the alternative: measured on real import text,
        # ``embed_batch`` is ~25% SLOWER than looping ``embed``. That workload
        # result is independent of the bounded physical micro-batch used by
        # llama.cpp, which still preserves the complete logical input.
        written = vector_store.write_episodic(
            text,
            tags=["imported", item.source_id],
            importance=float(item.payload["importance"]),
            source="import",
            preserve_existing=True,
            defer_embedding=True,
        )
        if written:
            return _WriteOutcome("imported")
        present = vector_store.has_episodic_text(text)
        return _WriteOutcome("existing" if present else "rejected")

    return _WriteOutcome("rejected")


def _same_schedule(job: Any, payload: dict[str, Any]) -> bool:
    if getattr(job, "name", "") != payload["name"]:
        return False
    if getattr(job, "message", "") != payload["message"]:
        return False
    if getattr(job, "timezone", "") != payload.get("timezone", ""):
        return False
    schedule = getattr(job, "schedule", None)
    if schedule is None:
        return False
    if "cron_expr" in payload:
        return getattr(schedule, "cron_expr", None) == payload["cron_expr"]
    if "every_secs" in payload:
        return getattr(schedule, "every_secs", None) == payload["every_secs"]
    return getattr(schedule, "at_ts", None) == payload.get("at_ts")


def _write_schedule(item: _Item, cron_service: Any) -> _WriteOutcome:
    payload = item.payload
    add_if_absent = getattr(cron_service, "add_job_if_absent", None)
    if callable(add_if_absent) and "add_job" not in vars(cron_service):
        job = add_if_absent(
            lambda candidate: _same_schedule(candidate, payload),
            name=payload["name"],
            message=payload["message"],
            every_secs=payload.get("every_secs"),
            at_ts=payload.get("at_ts"),
            cron_expr=payload.get("cron_expr"),
            created_by=f"import:{item.source_id}",
            enabled=False,
            timezone=payload.get("timezone", ""),
        )
        return _WriteOutcome("existing" if job is None else "imported")
    for job in cron_service.list_jobs(include_disabled=True):
        if _same_schedule(job, payload):
            return _WriteOutcome("existing")
    cron_service.add_job(
        name=payload["name"],
        message=payload["message"],
        every_secs=payload.get("every_secs"),
        at_ts=payload.get("at_ts"),
        cron_expr=payload.get("cron_expr"),
        created_by=f"import:{item.source_id}",
        enabled=False,
        timezone=payload.get("timezone", ""),
    )
    return _WriteOutcome("imported")


def apply_import(
    plan: dict[str, Any],
    *,
    data_home: Path | None = None,
    cron_service: Any = None,
    vector_store: VectorMemoryStore | None = None,
    lesson_store: Any = None,
    conflict_strategy: str = STRATEGY_SKIP,
) -> dict[str, Any]:
    """Apply selected source/category pairs with merge-only, idempotent writes.

    ``conflict_strategy`` decides what happens when a destination already holds a
    DIFFERENT item under the same identity. ``skip`` (the default) leaves it
    alone and reports a conflict; ``rename`` installs alongside it under a
    derived name; ``overwrite`` replaces it after writing a restore copy under
    ``imports/replaced/<timestamp>/``. Only ``skills``, ``mcp_servers``, and
    ``workspaces`` have resolvable collisions — the rest are merge-only.
    """
    destination = Path(data_home) if data_home is not None else config_dir()
    destination.mkdir(parents=True, exist_ok=True)
    selected = _selected_pairs(plan)
    roots = _plan_roots(plan)
    user_homes = _plan_user_homes(plan)
    config_paths = _plan_private_paths(plan, "_config_paths")
    workspace_paths = _plan_private_paths(plan, "_workspace_paths")
    strategy = _normalize_strategy(conflict_strategy)
    # One restore dir per apply run, so everything a single import replaced is
    # found together. Stamped once here rather than per item.
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ledger_path = destination / _LEDGER_RELATIVE_PATH
    ledger = _load_ledger(ledger_path)
    records = ledger["records"]
    # The ledger is rewritten WHOLE (atomic temp-file + rename), so flushing it
    # once per item is O(n**2) in serialization and rename cost for a large
    # import. Flush once per source/category instead, and once more in the
    # ``finally`` below, so an interrupted apply still cannot re-import an item
    # it already wrote.
    ledger_dirty = False
    imported = {category: 0 for category in CATEGORY_IDS}
    already_imported = 0
    item_outcomes: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = [
        item for item in plan.get("skipped", []) if isinstance(item, dict)
    ]
    scans: dict[str, _Scan] = {}
    # ONE registry snapshot for the whole apply. The rescan needs a READER, and
    # resolving one per source meant a provider that answered during the preview
    # but failed here produced `unknown_source`, returned success, and imported
    # nothing for a source the user had selected.
    registry = _sources()
    for source_id, category in sorted(selected):
        root = roots.get(source_id)
        source_configs = config_paths.get(source_id, ())
        if root is None or (
            not _source_exists(source_id, root)
            and not any(path.is_file() for path in source_configs)
        ):
            skipped.append(
                {
                    "source_id": source_id,
                    "category_id": category,
                    "reason": "source_unavailable",
                }
            )
            continue
        if source_id not in scans:
            scans[source_id] = _scan_source(
                source_id,
                root,
                user_homes.get(source_id, root.parent),
                config_paths=source_configs,
                workspace_paths=workspace_paths.get(source_id, ()),
                source=registry.get(source_id),
            )
            for diagnostic in scans[source_id].skipped:
                if diagnostic not in skipped:
                    skipped.append(diagnostic)

    if cron_service is None and any(category == "schedules" for _source, category in selected):
        from kiro_crew.cron import CronService

        cron_service = CronService(base_dir=destination)
    if lesson_store is None and any(category == "instructions" for _source, category in selected):
        lesson_store = LessonStore(base_dir=destination)

    owned_vector_store: VectorMemoryStore | None = None
    needs_vector_store = any(
        isinstance(item.payload, dict) and item.payload.get("kind") in ("semantic", "episodic")
        for scan in scans.values()
        for item in scan.items["memories"]
    )
    if vector_store is None and needs_vector_store:
        owned_vector_store = VectorMemoryStore(db_path=destination / "memory.db")
        owned_vector_store.embed_fn_factory = make_sync_embed_fn
        owned_vector_store.embed_fn = make_sync_embed_fn()
        owned_vector_store.init()
        vector_store = owned_vector_store

    def _flush_ledger() -> None:
        nonlocal ledger_dirty
        if ledger_dirty:
            _write_json(ledger_path, ledger)
            ledger_dirty = False

    try:
        for source_id, category in sorted(selected):
            scan = scans.get(source_id)
            if scan is None:
                continue
            for item in scan.items[category]:
                outcome = {
                    "source_id": source_id,
                    "category_id": category,
                    "item_hash": item.fingerprint,
                }
                # The ledger is a fast path, NOT the authority: it says "this
                # exact item was imported once", which is only equivalent to
                # "the destination still holds it" for categories that cannot be
                # replaced afterwards. For a single-occupancy destination an
                # overwrite (or a later revert) moves the destination out from
                # under an older fingerprint, so the writer's own destination
                # check has to decide. It reports ``existing`` when the item
                # really is already there, which lands as ``deduplicated`` all
                # the same.
                if item.fingerprint in records and category not in _REPLACEABLE_CATEGORIES:
                    already_imported += 1
                    item_outcomes.append({**outcome, "outcome": "deduplicated"})
                    continue
                written = _WriteOutcome("skipped")
                try:
                    if category == "instructions":
                        written = _write_instruction(item, lesson_store, vector_store)
                    elif category == "memories":
                        written = _write_memory(item, destination, vector_store)
                    elif category == "workspaces":
                        written = _write_workspace(
                            item,
                            destination,
                            strategy=strategy,
                        )
                    elif category == "mcp_servers":
                        written = _write_mcp(
                            item,
                            destination,
                            scan.user_home,
                            strategy=strategy,
                            run_stamp=run_stamp,
                        )
                    elif category == "skills":
                        written = _write_skill(
                            item,
                            destination,
                            strategy=strategy,
                            run_stamp=run_stamp,
                        )
                    elif category == "schedules":
                        written = _write_schedule(item, cron_service)
                    elif category == "settings":
                        written = _write_settings(item, destination)
                except (OSError, ValueError, TypeError, sqlite3.Error):
                    logger.warning(
                        "Foreign-agent import failed for %s/%s",
                        source_id,
                        category,
                        exc_info=True,
                    )
                    skipped.append(
                        {
                            "source_id": source_id,
                            "category_id": category,
                            "reason": "write_failed",
                        }
                    )
                    item_outcomes.append({**outcome, "outcome": "rejected"})
                    continue
                status = written.status
                # Only set when a strategy actually took effect, so a plain skip
                # apply reports exactly the shape it did before strategies existed.
                details = {
                    key: value
                    for key, value in (
                        ("renamed_to", written.renamed_to),
                        ("restored_to", written.restored_to),
                    )
                    if value
                }
                if status in ("imported", "existing"):
                    _record_ledger(
                        ledger,
                        item,
                        destination_key=written.destination_key,
                    )
                    ledger_dirty = True
                    if status == "imported":
                        imported[category] += 1
                        item_outcomes.append({**outcome, **details, "outcome": "accepted"})
                    else:
                        already_imported += 1
                        item_outcomes.append({**outcome, **details, "outcome": "deduplicated"})
                elif status == "conflict":
                    conflicts.append(
                        {
                            "source_id": source_id,
                            "category_id": category,
                            "reason": "destination_conflict",
                            # Tell the client which strategies could resolve this
                            # one, so a retry is an informed choice.
                            "resolvable": category in STRATEGY_CATEGORIES,
                        }
                    )
                    item_outcomes.append({**outcome, "outcome": "rejected"})
                else:
                    skipped.append(
                        {
                            "source_id": source_id,
                            "category_id": category,
                            "reason": "destination_rejected",
                        }
                    )
                    item_outcomes.append({**outcome, "outcome": "rejected"})
            _flush_ledger()
    finally:
        _flush_ledger()
        if owned_vector_store is not None:
            # This store is ours alone, so no caller can schedule the sweep that
            # fills the deferred vectors — run it here before closing. Blocking is
            # correct on this path: it is the non-interactive one (CLI, tests),
            # with no user watching a spinner.
            if imported["memories"]:
                with contextlib.suppress(Exception):
                    owned_vector_store.backfill_missing_embeddings()
            owned_vector_store.close()

    return {
        "imported": imported,
        "imported_count": sum(imported.values()),
        "already_imported": already_imported,
        # Episodic rows are written with a NULL embedding (see _write_memory), so
        # a caller holding a shared store MUST schedule
        # ``backfill_missing_embeddings`` off the request. Zero when this run
        # owned its store and already swept above.
        "embedding_backfill_pending": (imported["memories"] if owned_vector_store is None else 0),
        "item_outcomes": item_outcomes,
        "conflicts": conflicts,
        "skipped": skipped,
        "secret_count": max(
            int(plan.get("secret_count", 0)),
            sum(scan.secret_count for scan in scans.values()),
        ),
        "unsupported_count": max(
            int(plan.get("unsupported_count", 0)),
            sum(scan.unsupported_count for scan in scans.values()),
        ),
        "ledger": str(_LEDGER_RELATIVE_PATH).replace("\\", "/"),
        "conflict_strategy": strategy,
    }
