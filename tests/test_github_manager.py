from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from paperforge.github_manager import GitApprovalRequired, GitHubManager, GitResult


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


def test_git_manager_local_pr_release_citation_and_research_archive(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = GitHubManager(repo)
    manager.initialize()
    (repo / "README.md").write_text("# Research\n", encoding="utf-8")
    (repo / "result.json").write_text('{"score": 1}\n', encoding="utf-8")
    commit = manager.commit("initial")
    manager.create_branch("paper", start_point=commit)
    (repo / "paper.txt").write_text("draft\n", encoding="utf-8")
    manager.commit("paper", paths=("paper.txt",))

    pr = manager.create_pull_request(
        base="main",
        head="paper",
        title="Document result",
        body="Evidence-backed local record.",
    )
    assert json.loads(pr.read_text(encoding="utf-8"))["local_only"] is True
    manager.tag("v3.0.0-test")
    release = manager.publish_release(
        tag="v3.0.0-test",
        artifacts=(repo / "result.json",),
    )
    assert json.loads(release.read_text(encoding="utf-8"))["remote_published"] is False
    citation = manager.write_citation(
        {
            "title": "Research",
            "version": "3.0.0-test",
            "date-released": "2026-07-26",
        }
    )
    assert citation.is_file()

    first = manager.create_research_archive(
        tmp_path / "first.zip",
        paths=("README.md", "result.json"),
        metadata={"profile": "writing-only"},
    )
    second = manager.create_research_archive(
        tmp_path / "second.zip",
        paths=("result.json", "README.md"),
        metadata={"profile": "writing-only"},
    )
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        manifest = json.loads(archive.read("RESEARCH_ARCHIVE_MANIFEST.json"))
        assert [item["path"] for item in manifest["files"]] == [
            "README.md",
            "result.json",
        ]


def test_git_manager_remote_pr_and_release_require_and_record_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = GitHubManager(repo, allow_remote=True)
    manager.initialize()
    (repo / "tracked.txt").write_text("content\n", encoding="utf-8")
    manager.commit("initial")
    manager._run(
        ("remote", "add", "upstream", "https://github.com/example/research.git")
    )
    manager.create_branch("paper")
    (repo / "paper.txt").write_text("paper\n", encoding="utf-8")
    manager.commit("paper", paths=("paper.txt",))
    artifact = repo / "artifact.zip"
    artifact.write_bytes(b"archive")
    manager.tag("v3.0.0-test")

    with pytest.raises(GitApprovalRequired):
        manager.create_pull_request(
            base="main",
            head="paper",
            title="Remote PR",
            body="Approved publication.",
            publish=True,
        )

    calls: list[tuple[str, ...]] = []

    def fake_gh(args: tuple[str, ...]) -> GitResult:
        calls.append(args)
        kind = "pull/1" if args[:2] == ("pr", "create") else "releases/tag/v3.0.0-test"
        return GitResult(("gh", *args), 0, f"https://example.invalid/{kind}\n", "")

    monkeypatch.setattr(manager, "_run_gh", fake_gh)
    pr = manager.create_pull_request(
        base="main",
        head="paper",
        title="Remote PR",
        body="Approved publication.",
        publish=True,
        approved=True,
        remote="upstream",
    )
    release = manager.publish_release(
        tag="v3.0.0-test",
        artifacts=(artifact,),
        notes="Approved release.",
        publish=True,
        approved=True,
        remote="upstream",
    )

    assert json.loads(pr.read_text(encoding="utf-8"))["local_only"] is False
    assert json.loads(release.read_text(encoding="utf-8"))["remote_published"] is True
    assert [call[:2] for call in calls] == [("pr", "create"), ("release", "create")]
    assert all(call[-2:] == ("--repo", "github.com/example/research") for call in calls)


def test_research_archive_excludes_its_destination_from_allowlisted_directory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    bundle = repo / "bundle"
    bundle.mkdir(parents=True)
    manager = GitHubManager(repo)
    manager.initialize()
    (bundle / "result.json").write_text('{"score": 1}\n', encoding="utf-8")
    manager.commit("initial")
    destination = bundle / "research.zip"

    first = manager.create_research_archive(destination, paths=("bundle",)).read_bytes()
    second = manager.create_research_archive(destination, paths=("bundle",)).read_bytes()

    assert first == second
    with zipfile.ZipFile(destination) as archive:
        assert "bundle/research.zip" not in archive.namelist()


def test_research_archive_uses_one_scanned_snapshot_per_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "result.json"
    source.write_bytes(b'{"score": 1}\n')
    manager = GitHubManager(repo)
    manager.initialize()
    manager.commit("initial")
    original_read_bytes = Path.read_bytes
    source_reads = 0

    def changing_read_bytes(path: Path) -> bytes:
        nonlocal source_reads
        if path == source:
            source_reads += 1
            if source_reads > 1:
                return b"sk-" + (b"x" * 24)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", changing_read_bytes)
    destination = tmp_path / "research.zip"
    manager.create_research_archive(destination, paths=("result.json",))

    assert source_reads == 1
    with zipfile.ZipFile(destination) as archive:
        payload = archive.read("result.json")
        manifest = json.loads(archive.read("RESEARCH_ARCHIVE_MANIFEST.json"))
    assert payload == b'{"score": 1}\n'
    assert manifest["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_research_archive_excludes_case_alias_of_existing_destination(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    bundle = repo / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "result.json").write_text('{"score": 1}\n', encoding="utf-8")
    manager = GitHubManager(repo)
    manager.initialize()
    manager.commit("initial")
    lower = bundle / "research.zip"
    first = manager.create_research_archive(lower, paths=("bundle",)).read_bytes()
    alias = bundle / "RESEARCH.ZIP"
    if not alias.exists() or not os.path.samefile(lower, alias):
        pytest.skip("filesystem is case-sensitive")

    second = manager.create_research_archive(alias, paths=("bundle",)).read_bytes()

    assert first == second
