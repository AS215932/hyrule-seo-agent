"""Workspace guardrails: allowlist, unique-find, diff cap — the enforcement point."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.actions import workspace
from app.config import Settings
from app.models import DraftEdit


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "hyrule_web" / "templates").mkdir(parents=True)
    (tmp_path / "hyrule_web" / "templates" / "base.html").write_text(
        "<title>old title</title>\n<meta name='description' content='d'>\n"
    )
    (tmp_path / "hyrule_web" / "seo.py").write_text("SITE = 'x'\n")
    (tmp_path / "hyrule_web" / "app.py").write_text("app = 'forbidden'\n")
    return tmp_path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=str(tmp_path / "data"))


def test_apply_edits_happy_path(ws: Path, settings: Settings) -> None:
    changed = workspace.apply_edits(
        ws,
        settings,
        [DraftEdit(path="hyrule_web/templates/base.html", find="old title", replace="new title")],
    )
    assert changed == ["hyrule_web/templates/base.html"]
    assert "new title" in (ws / "hyrule_web/templates/base.html").read_text()


def test_rejects_paths_outside_allowlist(ws: Path, settings: Settings) -> None:
    with pytest.raises(workspace.WorkspaceError, match="allowlist"):
        workspace.apply_edits(
            ws, settings, [DraftEdit(path="hyrule_web/app.py", find="forbidden", replace="x")]
        )


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "hyrule_web/templates/../../x"])
def test_rejects_traversal(ws: Path, settings: Settings, path: str) -> None:
    with pytest.raises(workspace.WorkspaceError):
        workspace.apply_edits(ws, settings, [DraftEdit(path=path, find="a", replace="b")])


def test_rejects_missing_file(ws: Path, settings: Settings) -> None:
    with pytest.raises(workspace.WorkspaceError, match="does not exist"):
        workspace.apply_edits(
            ws, settings, [DraftEdit(path="hyrule_web/templates/nope.html", find="a", replace="b")]
        )


def test_rejects_symlink(ws: Path, settings: Settings) -> None:
    link = ws / "hyrule_web" / "templates" / "link.html"
    link.symlink_to(ws / "hyrule_web" / "app.py")
    with pytest.raises(workspace.WorkspaceError, match="symlink"):
        workspace.apply_edits(ws, settings, [DraftEdit(path="hyrule_web/templates/link.html", find="a", replace="b")])


def test_rejects_non_unique_find(ws: Path, settings: Settings) -> None:
    (ws / "hyrule_web" / "templates" / "base.html").write_text("dup\ndup\n")
    with pytest.raises(workspace.WorkspaceError, match="exactly 1"):
        workspace.apply_edits(
            ws, settings, [DraftEdit(path="hyrule_web/templates/base.html", find="dup", replace="x")]
        )


def test_rejects_absent_find(ws: Path, settings: Settings) -> None:
    with pytest.raises(workspace.WorkspaceError, match="occurs 0 times"):
        workspace.apply_edits(
            ws, settings, [DraftEdit(path="hyrule_web/templates/base.html", find="ghost", replace="x")]
        )


def test_rejects_oversized_diff_without_partial_writes(ws: Path, settings: Settings) -> None:
    big = "\n".join(f"line{i}" for i in range(400))
    with pytest.raises(workspace.WorkspaceError, match="cap"):
        workspace.apply_edits(
            ws,
            settings,
            [DraftEdit(path="hyrule_web/templates/base.html", find="old title", replace=big)],
        )
    # Atomicity: nothing was written even though validation happened per-edit.
    assert "old title" in (ws / "hyrule_web/templates/base.html").read_text()


def test_rejects_empty_edit_list(ws: Path, settings: Settings) -> None:
    with pytest.raises(workspace.WorkspaceError, match="no edits"):
        workspace.apply_edits(ws, settings, [])


async def test_validate_workspace_runs_configured_command(ws: Path, tmp_path: Path) -> None:
    settings = Settings(data_dir=str(tmp_path / "d"), workspace_validate_cmd="echo ok")
    result = await workspace.validate_workspace(ws, settings)
    assert result.ok
    assert "ok" in result.stdout
    settings_bad = Settings(data_dir=str(tmp_path / "d"), workspace_validate_cmd="exit 3")
    assert not (await workspace.validate_workspace(ws, settings_bad)).ok


async def test_ensure_workspace_clones_and_resets(tmp_path: Path) -> None:
    # A local bare "remote" keeps the test offline.
    import subprocess

    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "f.txt").write_text("v1")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c1"],
        cwd=origin,
        check=True,
    )
    settings = Settings(data_dir=str(tmp_path / "data"), git_remote_url=str(origin))
    path = await workspace.ensure_workspace(settings)
    assert (path / "f.txt").read_text() == "v1"
    # Local dirt is discarded on the next ensure.
    (path / "f.txt").write_text("dirty")
    path = await workspace.ensure_workspace(settings)
    assert (path / "f.txt").read_text() == "v1"
