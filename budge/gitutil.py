"""Git plumbing for the data repo. Shell-outs to stock git only."""

from __future__ import annotations

from pathlib import Path

from .util import dry, run, warn


def git(repo: Path, *args, check=True):
    return run(["git", "-C", str(repo)] + [str(a) for a in args], check=check)


def ensure_repo(repo: Path) -> None:
    if not (repo / ".git").exists():
        if dry(f"git init {repo}"):
            return
        run(["git", "init", "-b", "main", str(repo)])


def commit_all(repo: Path, message: str) -> bool:
    """Stage everything and commit atomically. Returns True if a commit was made."""
    if dry(f"git commit -am {message!r} in {repo}"):
        return False
    git(repo, "add", "-A")
    status = git(repo, "status", "--porcelain").stdout.strip()
    if not status:
        return False
    git(repo, "commit", "-m", message, "-q")
    return True


def push(repo: Path) -> None:
    if dry(f"git push from {repo}"):
        return
    remotes = git(repo, "remote").stdout.split()
    if not remotes:
        warn("no git remote configured; skipping push")
        return
    # First-ever push has no upstream tracking; set it rather than fail.
    has_upstream = git(repo, "rev-parse", "--abbrev-ref",
                       "--symbolic-full-name", "@{u}", check=False
                       ).returncode == 0
    if has_upstream:
        git(repo, "push", "-q")
    else:
        branch = git(repo, "rev-parse", "--abbrev-ref",
                     "HEAD").stdout.strip() or "main"
        git(repo, "push", "-q", "-u", remotes[0], branch)


def head_commit(repo: Path) -> str:
    proc = git(repo, "rev-parse", "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""
