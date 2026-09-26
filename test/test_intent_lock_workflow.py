"""Behavioural tests for .github/scripts/intent-lock.sh (Intent Lock).

The script runs for real with ``gh`` replaced by a stub that serves canned API
answers and records every write, so each property below is observed rather
than read off the source:

* the hash covers exactly the frozen sections (Goal line, Why it matters, Not a
  goal) and ignores CRLF, trailing-space and blank-line churn;
* ``evaluate`` (run by PR Readiness, the required status) posts a bot baseline
  on ``opened`` and compares the CURRENT body with it on every later event;
* only a github-actions[bot] comment counts as a baseline;
* no baseline on a PR opened after the script landed fails closed; an older PR
  is skipped;
* ``approve`` is honoured only from a writer, only for the head, only for the
  body the comment saw.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "intent-lock.sh"
READINESS = ROOT / ".github" / "workflows" / "pr-readiness.yml"
APPROVE = ROOT / ".github" / "workflows" / "intent-lock.yml"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="requires a POSIX bash and jq",
)

HEAD = "a686d96a83859a73eb93b322de04b21bdea5f093"
BOT = "github-actions[bot]"
SHEBANG = "#!" + "/usr/bin/env bash"
# The commit that added the script to the default branch.
LANDED = "2026-09-20T00:00:00Z"

BODY = """\
## Problem / Motivation

**Goal:** the thing works again.

Something is broken.

## Why it matters

Users hit it daily.

## Not a goal

- Rewriting the thing.

## What changed (motivation -> approach -> change)

A small fix.
"""
CHANGED = BODY.replace("works again", "is rewritten")

# The stub applies --jq itself (gh does), logs every write to $FIXTURES/calls,
# and keeps each write's --input body as $FIXTURES/input.<n>.
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "workflow" ]; then
  echo "DISPATCH $*" >> "$FIXTURES/calls"
  [ -f "$FIXTURES/dispatch_fail" ] && exit 1
  exit 0
fi
shift
method=GET; url=""; jqf=""
while [ $# -gt 0 ]; do
  case "$1" in
    --method) method="$2"; shift ;;
    --jq) jqf="$2"; shift ;;
    --input)
      n=$(ls "$FIXTURES" | grep -c '^input\.' || true)
      cat > "$FIXTURES/input.$n"; shift ;;
    repos/*) url="$1" ;;
  esac
  shift
done
if [ "$method" != GET ]; then
  echo "$method $url" >> "$FIXTURES/calls"
  [ -f "$FIXTURES/write_fail" ] && { echo "gh: Server Error (HTTP 502)" >&2; exit 1; }
  exit 0
fi
case "$url" in
  */pulls/*) file=pr.json ;;
  */issues/*/comments*) file=comments.json ;;
  *commits\?path=*) file=commits.json ;;
  */permission)
    if [ -f "$FIXTURES/perm_fail" ]; then echo "gh: Server Error (HTTP 502)" >&2; exit 1; fi
    file=permission.json ;;
  *) echo "gh stub: unhandled $url" >&2; exit 90 ;;
esac
[ -f "$FIXTURES/$file" ] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
if [ -n "$jqf" ]; then jq -r "$jqf" "$FIXTURES/$file"; else cat "$FIXTURES/$file"; fi
"""


def frozen_hash(body: str) -> str:
    out = subprocess.run(
        ["bash", str(SCRIPT), "--hash"],
        env={**os.environ, "PR_BODY": body},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return out.stdout.strip()


def baseline_comment(body: str, *, author: str = BOT, cid: int = 501) -> dict:
    return {
        "id": cid,
        "user": {"login": author},
        "body": f"<!-- intent-lock baseline={frozen_hash(body)} -->\nIntent Lock baseline",
    }


class Repo:
    def __init__(self, root: Path) -> None:
        self.fixtures = root / "fixtures"
        bindir = root / "bin"
        self.fixtures.mkdir()
        bindir.mkdir()
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        # The baseline post is retried after a real `sleep`; nothing waits on it.
        nap = bindir / "sleep"
        nap.write_text(SHEBANG + "\nexit 0\n")
        nap.chmod(0o755)
        self.output = root / "github_output"
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "GH_TOKEN": "x",
            "REPO": "kirodotdev/KiroCrew",
            "PR": "7",
            "SHA": HEAD,
            "DEFAULT_BRANCH": "main",
            "GITHUB_OUTPUT": str(self.output),
        }
        (self.fixtures / "commits.json").write_text(
            json.dumps([{"commit": {"committer": {"date": LANDED}}}])
        )
        self.set(body=BODY, comments=[])

    def set(self, *, body=None, comments=None, permission=None, created=None) -> None:
        if body is not None:
            self.body = body
        if body is not None or created is not None:
            (self.fixtures / "pr.json").write_text(
                json.dumps(
                    {
                        "head": {"sha": HEAD},
                        "body": self.body,
                        "created_at": created or "2026-09-25T00:00:00Z",
                    }
                )
            )
        if comments is not None:
            (self.fixtures / "comments.json").write_text(json.dumps(comments))
        if permission is not None:
            (self.fixtures / "permission.json").write_text(json.dumps({"permission": permission}))

    def _run(self, mode: str, **extra: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        self.output.write_text("")
        (self.fixtures / "calls").unlink(missing_ok=True)  # calls are per run
        proc = subprocess.run(
            ["bash", str(SCRIPT), mode],
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.fixtures,
        )
        calls_file = self.fixtures / "calls"
        calls = calls_file.read_text().splitlines() if calls_file.exists() else []
        return proc, calls

    def evaluate(
        self, action: str = "edited", event: str = "pull_request_target", **extra: str
    ) -> tuple[str, list[str]]:
        env = {"EVENT": event, "ACTION": action, "EVENT_BODY": self.body, **extra}
        proc, calls = self._run("evaluate", **env)
        assert proc.returncode == 0, proc.stderr
        [state] = [ln for ln in self.output.read_text().splitlines() if ln.startswith("state=")]
        return state.removeprefix("state="), calls

    def approve(self, sha: str, user: str = "maint", **extra: str) -> list[str]:
        proc, calls = self._run(
            "approve",
            COMMENT_BODY=f"/intent approve {sha}",
            COMMENT_USER=user,
            EVENT_BODY=extra.pop("EVENT_BODY", self.body),
            **extra,
        )
        assert proc.returncode == 0, proc.stderr
        return calls

    def inputs(self) -> list[str]:
        return [
            json.loads((self.fixtures / f"input.{n}").read_text())["body"]
            for n in range(len(list(self.fixtures.glob("input.*"))))
        ]


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


class TestFrozenHash:
    def test_formatting_churn_is_not_a_goal_change(self) -> None:
        churned = BODY.replace("\n", "  \r\n").replace(
            "Users hit it daily.", "\nUsers hit it daily.\n"
        )
        assert frozen_hash(churned) == frozen_hash(BODY)

    @pytest.mark.parametrize(
        "old,new",
        [
            ("the thing works again.", "the thing is rewritten."),
            ("Users hit it daily.", "Nobody hits it."),
            ("- Rewriting the thing.", "- Nothing."),
        ],
    )
    def test_each_frozen_section_is_covered(self, old: str, new: str) -> None:
        assert frozen_hash(BODY.replace(old, new)) != frozen_hash(BODY)

    def test_a_renamed_frozen_heading_is_a_change(self) -> None:
        renamed = BODY.replace("## Not a goal", "## Not a goal (superseded, see below)")
        assert frozen_hash(renamed) != frozen_hash(BODY)

    def test_sections_outside_the_goal_are_not_covered(self) -> None:
        assert frozen_hash(BODY.replace("A small fix.", "A large fix.")) == frozen_hash(BODY)
        assert frozen_hash(BODY.replace("Something is broken.", "Other.")) == frozen_hash(BODY)

    def test_fenced_text_inside_a_frozen_section_is_covered(self) -> None:
        def body(word: str) -> str:
            return BODY.replace("Users hit it daily.", f"Users hit it daily.\n\n```\n{word}\n```")

        assert frozen_hash(body("one")) != frozen_hash(body("two"))

    def test_text_in_a_code_span_that_looks_like_a_comment_is_goal_text(self) -> None:
        # Visible `<!-- old -->` text must not vanish from the hash.
        def body(word: str) -> str:
            return BODY.replace("Users hit it daily.", f"Users hit `<!-- {word} -->` daily.")

        assert frozen_hash(body("old")) != frozen_hash(body("new"))

    def test_a_fence_info_string_is_goal_text(self) -> None:
        def body(info: str) -> str:
            return BODY.replace("Users hit it daily.", f"Users hit it daily.\n\n```{info}\nx\n```")

        assert frozen_hash(body("text")) != frozen_hash(body("mermaid"))

    def test_a_fenced_heading_does_not_open_a_frozen_section(self) -> None:
        fenced = BODY + "\n```\n## Not a goal\n- smuggled\n```\n"
        assert frozen_hash(fenced) == frozen_hash(BODY)


class TestEvaluate:
    """What PR Readiness sees on every event it runs on."""

    def test_opening_posts_the_opened_body_as_a_bot_baseline(self, repo: Repo) -> None:
        state, calls = repo.evaluate(action="opened")
        assert state == "ok"
        assert calls == ["POST repos/kirodotdev/KiroCrew/issues/7/comments"]
        [posted] = repo.inputs()
        assert posted.startswith(f"<!-- intent-lock baseline={frozen_hash(BODY)} -->")

    def test_an_edit_before_the_opened_read_is_changed_not_the_baseline(self, repo: Repo) -> None:
        repo.set(body=CHANGED)
        state, _ = repo.evaluate(action="opened", EVENT_BODY=BODY)
        assert state == "changed"
        assert repo.inputs()[0].startswith(f"<!-- intent-lock baseline={frozen_hash(BODY)} -->")

    def test_a_goal_written_after_opening_needs_approval(self, repo: Repo) -> None:
        # No lazy "first filled Goal" baseline: two quick edits could otherwise
        # freeze the second one unapproved.
        empty = BODY.replace("the thing works again.", "")
        repo.set(body=empty)
        repo.evaluate(action="opened")
        assert repo.inputs()[0].startswith(f"<!-- intent-lock baseline={frozen_hash(empty)} -->")
        repo.set(comments=[baseline_comment(empty)], body=BODY)
        assert repo.evaluate()[0] == "changed"

    def test_an_unchanged_goal_is_ok_and_writes_nothing(self, repo: Repo) -> None:
        repo.set(comments=[baseline_comment(BODY)])
        assert repo.evaluate() == ("ok", [])

    def test_a_changed_goal_is_changed_on_any_event(self, repo: Repo) -> None:
        # The required status itself compares the CURRENT body, so an edit on
        # an already-green head is caught by the next readiness run, with no
        # separate check that could still be pending or stale (F2).
        repo.set(comments=[baseline_comment(BODY)], body=CHANGED)
        for event in ("pull_request_target", "workflow_dispatch", "workflow_run"):
            state, calls = repo.evaluate(event=event)
            assert state == "changed"
            assert calls == []

    def test_only_a_bot_comment_counts_as_the_baseline(self, repo: Repo) -> None:
        # A forged marker matching the NEW text, posted after the real one.
        repo.set(
            body=CHANGED,
            comments=[baseline_comment(BODY), baseline_comment(CHANGED, author="mallory", cid=600)],
        )
        assert repo.evaluate()[0] == "changed"

    def test_other_bot_comments_are_not_baselines(self, repo: Repo) -> None:
        # PRs carry many github-actions[bot] comments (review verdicts, notices).
        other = {"id": 300, "user": {"login": BOT}, "body": "<!-- codex-ai-review -->\nverdict"}
        repo.set(comments=[other, baseline_comment(BODY), {**other, "id": 700}])
        assert repo.evaluate()[0] == "ok"

    def test_a_new_pr_with_no_baseline_fails_closed(self, repo: Repo) -> None:
        # Opened after the script landed, so a baseline was posted and is now
        # gone -- deleted, or the opening run never recorded it (F1).
        state, calls = repo.evaluate()
        assert state == "missing"
        assert calls == []

    def test_an_older_pr_with_no_baseline_is_skipped(self, repo: Repo) -> None:
        repo.set(created="2026-09-01T00:00:00Z")
        assert repo.evaluate() == ("skip", [])

    @pytest.mark.parametrize("fixture", ["pr.json", "comments.json", "commits.json"])
    def test_a_failed_read_is_unreadable_never_skip(self, repo: Repo, fixture: str) -> None:
        (repo.fixtures / fixture).unlink()
        assert repo.evaluate()[0] == "unreadable"

    def test_a_failed_baseline_post_is_retried_then_fails_closed(self, repo: Repo) -> None:
        (repo.fixtures / "write_fail").write_text("")
        state, calls = repo.evaluate(action="opened")
        assert state == "missing"
        assert calls == ["POST repos/kirodotdev/KiroCrew/issues/7/comments"] * 2

    def _red(self, repo: Repo) -> None:
        repo.set(body=CHANGED, comments=[baseline_comment(BODY)])

    def test_a_writer_approving_the_head_moves_the_baseline(self, repo: Repo) -> None:
        self._red(repo)
        repo.set(permission="write")
        calls = repo.approve(HEAD[:12])
        assert calls[0] == "POST repos/kirodotdev/KiroCrew/issues/7/comments"
        assert repo.inputs()[0].startswith(f"<!-- intent-lock baseline={frozen_hash(CHANGED)} -->")
        assert (
            f"DISPATCH workflow run pr-readiness.yml --repo kirodotdev/KiroCrew -f pr=7 -f sha={HEAD}"
            in calls
        )
        repo.set(comments=[baseline_comment(BODY), baseline_comment(CHANGED, cid=502)])
        assert repo.evaluate()[0] == "ok"  # the latest bot baseline counts

    def test_a_failed_readiness_dispatch_fails_the_job(self, repo: Repo) -> None:
        self._red(repo)
        repo.set(permission="write")
        (repo.fixtures / "dispatch_fail").write_text("")
        proc, calls = repo._run(
            "approve",
            COMMENT_BODY=f"/intent approve {HEAD}",
            COMMENT_USER="maint",
            EVENT_BODY=CHANGED,
        )
        assert proc.returncode == 1
        assert calls[0] == "POST repos/kirodotdev/KiroCrew/issues/7/comments"

    def test_an_uppercase_sha_is_accepted(self, repo: Repo) -> None:
        self._red(repo)
        repo.set(permission="write")
        calls = repo.approve(HEAD[:12].upper())
        assert any(c.startswith("DISPATCH") for c in calls)

    def test_approving_a_pr_with_no_baseline_posts_one(self, repo: Repo) -> None:
        repo.set(permission="admin")
        calls = repo.approve(HEAD)
        assert calls[0] == "POST repos/kirodotdev/KiroCrew/issues/7/comments"
        assert repo.inputs()[0].startswith(f"<!-- intent-lock baseline={frozen_hash(BODY)} -->")

    @pytest.mark.parametrize("permission", ["read", "triage", None])
    def test_a_non_writer_is_refused(self, repo: Repo, permission: str | None) -> None:
        self._red(repo)
        if permission:
            repo.set(permission=permission)
        calls = repo.approve(HEAD)
        assert not [c for c in calls if c.startswith("DISPATCH")]
        assert "Only a repository writer" in repo.inputs()[0]

    def test_an_unreadable_permission_is_not_called_a_non_writer(self, repo: Repo) -> None:
        self._red(repo)
        (repo.fixtures / "perm_fail").write_text("")
        calls = repo.approve(HEAD)
        assert not [c for c in calls if c.startswith("DISPATCH")]
        assert "GitHub did not confirm it" in repo.inputs()[0]

    def test_a_stale_sha_is_refused(self, repo: Repo) -> None:
        self._red(repo)
        repo.set(permission="admin")
        calls = repo.approve("b" * 40)
        assert not [c for c in calls if c.startswith("DISPATCH")]
        assert HEAD in repo.inputs()[0]

    def test_a_goal_edited_after_the_approval_comment_is_refused(self, repo: Repo) -> None:
        self._red(repo)
        repo.set(permission="write")
        calls = repo.approve(HEAD, EVENT_BODY=BODY.replace("works again", "is approved text"))
        assert not [c for c in calls if c.startswith("DISPATCH")]
        assert "changed after the comment" in repo.inputs()[0]

    def test_an_unrelated_comment_does_nothing(self, repo: Repo) -> None:
        self._red(repo)
        proc, calls = repo._run("approve", COMMENT_BODY="/intent approve please", COMMENT_USER="x")
        assert proc.returncode == 0
        assert calls == []


class TestWiring:
    def test_readiness_evaluates_the_goal_from_the_default_branch_before_the_verdict(self) -> None:
        steps = yaml.safe_load(READINESS.read_text(encoding="utf-8"))["jobs"]["readiness"]["steps"]
        ids = [s.get("id") for s in steps]
        intent = steps[ids.index("intent")]
        assert intent["run"].strip() == "bash .github/scripts/intent-lock.sh evaluate"
        assert ids.index("intent") < ids.index("verdict")
        assert (
            steps[ids.index("verdict")]["env"]["INTENT_STATE"]
            == "${{ steps.intent.outputs.state }}"
        )
        checkout = next(s for s in steps if "sparse-checkout" in (s.get("with") or {}))
        assert ".github/scripts" in checkout["with"]["sparse-checkout"].split()
        assert steps.index(checkout) < ids.index("intent")

    def test_a_stale_opened_run_still_records_the_baseline(self) -> None:
        # A push right after opening makes the `opened` run stale, which skips
        # the verdict. The baseline must still come from the body it opened
        # with, or every later event would read "no goal baseline".
        steps = yaml.safe_load(READINESS.read_text(encoding="utf-8"))["jobs"]["readiness"]["steps"]
        checkout = next(s for s in steps if "sparse-checkout" in (s.get("with") or {}))
        intent = next(s for s in steps if s.get("id") == "intent")
        for step in (checkout, intent):
            assert "github.event.action == 'opened'" in step["if"]

    def test_no_separate_intent_lock_check_run_is_published(self) -> None:
        assert "check-runs" not in SCRIPT.read_text(encoding="utf-8")
        spec = yaml.safe_load(APPROVE.read_text(encoding="utf-8"))
        assert list(spec[True]) == ["issue_comment"]
