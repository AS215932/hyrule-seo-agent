"""hyrule-web workspace: clone/refresh, guarded edit application, validation, push.

This module is the enforcement point for the code-level guardrails — the
drafter's output is only ever applied through ``apply_edits``:

- path allowlist (settings.allowed_edit_paths), no traversal, no symlinks;
- each ``find`` must occur exactly once in its file;
- total changed-line budget (settings.max_diff_lines);
- hyrule-web's own lint+tests must pass in the workspace before any push.

Git runs via subprocess in a thread; no shell interpolation anywhere.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

from app.config import Settings
from app.models import DraftEdit

log = structlog.get_logger()


class WorkspaceError(RuntimeError):
    """Raised when a guarded workspace operation must abort the draft cycle."""


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str
    stderr: str


def workspace_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / "workspace" / "hyrule-web"


async def _run(args: list[str], *, cwd: Path, timeout: float = 600.0) -> CommandResult:
    def _call() -> CommandResult:
        completed = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return CommandResult(completed.returncode == 0, completed.stdout, completed.stderr)

    return await asyncio.to_thread(_call)


async def ensure_workspace(settings: Settings) -> Path:
    """Clone on first use; hard-reset to origin/<default> otherwise."""
    path = workspace_path(settings)
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        result = await _run(
            ["git", "clone", "--depth", "50", settings.git_remote_url, str(path)],
            cwd=path.parent,
        )
        if not result.ok:
            raise WorkspaceError(f"clone failed: {result.stderr.strip()[:400]}")
        return path
    for args in (
        ["git", "fetch", "origin", settings.git_default_branch],
        ["git", "checkout", "-f", settings.git_default_branch],
        ["git", "reset", "--hard", f"origin/{settings.git_default_branch}"],
        ["git", "clean", "-fd"],
    ):
        result = await _run(args, cwd=path)
        if not result.ok:
            raise WorkspaceError(f"{' '.join(args[:2])} failed: {result.stderr.strip()[:400]}")
    return path


def _validate_path(workspace: Path, settings: Settings, rel_path: str) -> Path:
    if rel_path.startswith(("/", "~")) or ".." in rel_path.split("/"):
        raise WorkspaceError(f"edit path escapes the workspace: {rel_path}")
    if not any(rel_path.startswith(prefix) for prefix in settings.allowed_edit_path_list):
        raise WorkspaceError(f"edit path outside the allowlist: {rel_path}")
    target = workspace / rel_path
    if target.is_symlink():
        raise WorkspaceError(f"edit path is a symlink: {rel_path}")
    if not target.is_file():
        raise WorkspaceError(f"edit path does not exist: {rel_path}")
    return target


def apply_edits(workspace: Path, settings: Settings, edits: list[DraftEdit]) -> list[str]:
    """Apply validated find/replace edits; return changed file list."""
    if not edits:
        raise WorkspaceError("draft proposed no edits")
    changed_lines = 0
    staged: list[tuple[Path, str]] = []
    for edit in edits:
        target = _validate_path(workspace, settings, edit.path)
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(edit.find)
        if occurrences != 1:
            raise WorkspaceError(
                f"find-string occurs {occurrences} times (need exactly 1) in {edit.path}"
            )
        changed_lines += max(len(edit.find.splitlines()), len(edit.replace.splitlines()))
        staged.append((target, text.replace(edit.find, edit.replace, 1)))
    if changed_lines > settings.max_diff_lines:
        raise WorkspaceError(
            f"draft touches ~{changed_lines} lines (cap {settings.max_diff_lines})"
        )
    for target, new_text in staged:
        target.write_text(new_text, encoding="utf-8")
    return sorted({edit.path for edit in edits})


async def validate_workspace(workspace: Path, settings: Settings) -> CommandResult:
    """Run hyrule-web's own gate inside the workspace; red aborts the PR."""
    return await _run(
        ["sh", "-c", settings.workspace_validate_cmd], cwd=workspace, timeout=1800.0
    )


async def commit_and_push(
    workspace: Path,
    settings: Settings,
    token: str,
    *,
    branch: str,
    title: str,
) -> None:
    """Create the branch, commit the applied edits, push with the App token."""
    push_url = f"https://x-access-token:{token}@github.com/{settings.github_repo}.git"
    for args in (
        ["git", "checkout", "-B", branch],
        ["git", "-c", "user.name=hyrule-seo-agent", "-c", "user.email=seo-agent@hyrule.host",
         "commit", "-am", f"seo: {title}\n\nOpened by seo-agent; human review required."],
        ["git", "push", "-u", push_url, branch],
    ):
        result = await _run(args, cwd=workspace)
        if not result.ok:
            # Never echo stderr containing the tokenised URL.
            detail = result.stderr.replace(token, "***").strip()[:400]
            raise WorkspaceError(f"{' '.join(args[:2])} failed: {detail}")
