"""Team sharing of the Appointments / Analyze data through a git branch.

The publisher's app writes the shared tables as one JSON file and commits it to its own
branch (``openmile-shared``) with git plumbing, so the code branch and the working tree
are never touched; viewers fetch that branch and load the file read-only.  Git must be
installed and the repository cloned (``git clone``); a first manual ``git push`` stores
the GitHub login the background pushes then use.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

FILE = "openmile_shared.json"


class ShareError(RuntimeError):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail or message


def _git(root: Path, *args: str, stdin: str | None = None, timeout: int = 60) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang on a login prompt
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        env.setdefault(k, "OpenMile")  # commits are made even where git has no user set
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        env.setdefault(k, "openmile@localhost")
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=root,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ShareError("git is not installed on this PC.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ShareError(f"git {args[0]} took too long (network?).") from exc
    if out.returncode != 0:
        text = (out.stderr or out.stdout).strip()
        first = next((ln for ln in text.splitlines() if not ln.startswith("hint:")), text)
        raise ShareError(f"git {args[0]} failed: {first or out.returncode}", text)
    return out.stdout.strip()


def fingerprint(data: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def is_git_clone(root: Path) -> bool:
    return (root / ".git").exists()


class ShareConflictError(ShareError):
    """Someone else pushed first: the push was refused (nothing was overwritten)."""


def publish(
    root: Path, data: dict[str, Any], branch: str, parent: str | None, remote: str = "origin"
) -> str:
    """Commit ``data`` on top of ``parent`` (the team commit this PC last loaded; None for
    the first one) and push it.  If the branch moved on since, git refuses the push and
    ShareConflictError is raised.  Returns the new commit id."""
    if not is_git_clone(root):
        raise ShareError("This folder is not a git clone (it was downloaded as a ZIP).")
    payload = json.dumps(data, sort_keys=True, default=str, indent=1)
    blob = _git(root, "hash-object", "-w", "--stdin", stdin=payload)
    tree = _git(root, "mktree", stdin=f"100644 blob {blob}\t{FILE}\n")
    commit = _git(
        root,
        "commit-tree",
        tree,
        *(["-p", parent] if parent else []),
        "-m",
        "OpenMile appointments / analyze update",
    )
    try:
        _git(root, "push", "--quiet", remote, f"{commit}:refs/heads/{branch}", timeout=120)
    except ShareError as exc:
        text = exc.detail.lower()
        if "rejected" in text or "non-fast-forward" in text or "fetch first" in text:
            raise ShareConflictError("Someone else saved just now.") from exc
        raise
    return commit


def fetch(root: Path, branch: str, remote: str = "origin") -> tuple[str, dict[str, Any]] | None:
    """The team branch's commit id and data (None when nothing was published yet)."""
    if not is_git_clone(root):
        raise ShareError("This folder is not a git clone (it was downloaded as a ZIP).")
    try:
        _git(root, "fetch", "--quiet", remote, branch)
    except ShareError as exc:
        if "couldn't find remote ref" in exc.detail.lower():
            return None
        raise
    commit = _git(root, "rev-parse", "FETCH_HEAD")
    return commit, json.loads(_git(root, "show", f"{commit}:{FILE}"))


def sync(
    repo: Any,
    root: Path,
    role: str,
    branch: str,
    remote: str = "origin",
    pull: bool = True,
) -> str:
    """One round of team sharing; returns a short status for the page.

    viewer: load the team's data when it changed.
    editor: first load what others saved (``pull``), then push this PC's own changes.  A
    change made here while someone else saved is not pushed (git refuses it); the team's
    data is loaded instead and the status says to redo it.
    """
    head = repo.get_state("share_head")  # the team commit this PC last loaded / pushed
    pushed_fp = repo.get_state("share_fp")  # what this PC's shared tables were then
    local = repo.export_shared()
    local_fp = fingerprint(local)
    changed_here = pushed_fp is not None and local_fp != pushed_fp
    if pull or role == "viewer":
        team = fetch(root, branch, remote)
        if team is not None and team[0] != head:
            if role == "editor" and changed_here and head is not None:
                status = "conflict"  # both changed: the team wins, nothing overwritten
            else:
                status = "loaded"
            repo.import_shared(team[1])
            repo.set_state("share_head", team[0])
            repo.set_state("share_fp", fingerprint(repo.export_shared()))
            return status
        if team is None and role == "viewer":
            return "empty"
    if role != "editor":
        return "same"
    if pushed_fp is not None and not changed_here:
        return "same"
    try:
        commit = publish(root, local, branch, head, remote)
    except ShareConflictError:
        team = fetch(root, branch, remote)
        if team is not None:
            repo.import_shared(team[1])
            repo.set_state("share_head", team[0])
            repo.set_state("share_fp", fingerprint(repo.export_shared()))
        return "conflict"
    repo.set_state("share_head", commit)
    repo.set_state("share_fp", local_fp)
    return "pushed"
