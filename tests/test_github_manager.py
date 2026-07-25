from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from paperforge.github_manager import GitApprovalRequired, GitHubManager


def test_git_manager_local_release_and_explicit_remote_approval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = GitHubManager(repo)
    manager.initialize()
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    commit = manager.commit("initial")
    assert len(commit) == 40
    assert manager.tag("v1.0.0") == commit

    with pytest.raises(GitApprovalRequired):
        manager.push(remote="origin", refspec="main", approved=False)


def test_git_manager_can_push_to_approved_local_bare_remote(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(("git", "init", "--bare", str(remote)), check=True, capture_output=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = GitHubManager(repo, allow_remote=True)
    manager.initialize()
    (repo / "tracked.txt").write_text("content\n", encoding="utf-8")
    manager.commit("initial")
    manager._run(("remote", "add", "origin", remote.as_uri()))

    result = manager.push(remote="origin", refspec="main", approved=True)
    assert result.success
    assert (
        subprocess.run(
            ("git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == manager.head()
    )
