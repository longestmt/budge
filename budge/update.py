"""Explicit, release-based self-update for the pipx-installed Budge CLI.

Only stable vX.Y.Z tags from the official code repository are considered.
The tag is resolved to a commit before installation so the exact reviewed
revision is installed even if a tag were subsequently moved.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import sys

from . import __version__
from . import util
from .util import say, success, warn


SOURCE_URL = "https://github.com/longestmt/budge.git"
_VERSION_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Release:
    version: str
    version_key: tuple[int, int, int]
    tag: str
    commit: str


def _version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch("v" + value.removeprefix("v"))
    if not match:
        raise RuntimeError(f"invalid Budge version: {value}")
    return tuple(int(part) for part in match.groups())


def _parse_releases(output: str) -> list[Release]:
    """Parse ls-remote output, preferring peeled annotated-tag commits."""
    refs: dict[str, tuple[str, bool]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        commit, ref = parts
        if not _COMMIT_RE.fullmatch(commit):
            continue
        peeled = ref.endswith("^{}")
        tag = ref[len("refs/tags/"):].removesuffix("^{}")
        if not _VERSION_RE.fullmatch(tag):
            continue
        if tag not in refs or peeled:
            refs[tag] = (commit, peeled)
    return sorted(
        (Release(tag[1:], _version_key(tag), tag, commit)
         for tag, (commit, _) in refs.items()),
        key=lambda release: release.version_key,
    )


def available_releases() -> list[Release]:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is required to check for Budge updates")
    proc = util.run(
        [git, "ls-remote", "--tags", SOURCE_URL, "refs/tags/v*"],
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"could not check for Budge updates: {detail}")
    return _parse_releases(proc.stdout)


def _release_for_version(releases: list[Release], version: str):
    key = _version_key(version)
    return next((release for release in releases
                 if release.version_key == key), None)


def _install(pipx: str, release: Release):
    spec = f"git+{SOURCE_URL}@{release.commit}"
    return util.run([pipx, "install", "--force", spec], check=False)


def _rollback(pipx: str, previous: Release | None) -> str:
    if previous is None:
        return "no matching previous release tag was available"
    restored = _install(pipx, previous)
    if restored.returncode == 0:
        return f"restored {previous.tag}"
    detail = restored.stderr.strip() or restored.stdout.strip()
    return f"rollback to {previous.tag} failed: {detail}"


def _refresh_units(budge_bin: str) -> None:
    proc = util.run([budge_bin, "setup", "--units-only"], check=False)
    if proc.stdout.strip():
        say(proc.stdout.strip())
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "unknown error"
        warn("Budge updated, but service files were not refreshed: " + detail)


def _refresh_manpage() -> None:
    source = Path(sys.prefix) / "share" / "man" / "man1" / "budge.1"
    target = Path("/usr/local/share/man/man1/budge.1")
    if not source.exists():
        warn("Budge updated, but the packaged man page was not found")
        return
    writable = ((target.exists() and target.is_file()
                 and os.access(target, os.W_OK))
                or (target.parent.exists()
                    and os.access(target.parent, os.W_OK)))
    if not writable:
        say("Man page update needs administrator access:\n"
            f"  sudo install -m 0644 {source} {target}\n"
            "  sudo mandb -q")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    mandb = shutil.which("mandb")
    if mandb:
        util.run([mandb, "-q"], check=False)


def run_update(check_only: bool = False,
               refresh_services: bool = True) -> None:
    releases = available_releases()
    if not releases:
        say("No stable Budge releases have been published yet.")
        return

    current_key = _version_key(__version__)
    latest = releases[-1]
    say(f"installed: {__version__}")
    say(f"latest stable: {latest.version} ({latest.tag})")
    if current_key >= latest.version_key:
        if current_key == latest.version_key:
            success("Budge is up to date")
        else:
            say("This Budge build is newer than the latest stable release.")
        return

    say(f"update available: {__version__} -> {latest.version}")
    if check_only:
        return
    if util.dry(f"install Budge {latest.tag} ({latest.commit[:12]})"):
        return

    pipx = shutil.which("pipx")
    if not pipx:
        raise RuntimeError("pipx is required to update Budge")
    budge_bin = shutil.which("budge")
    if not budge_bin:
        raise RuntimeError("could not locate the installed budge command")

    previous = _release_for_version(releases, __version__)
    installed = _install(pipx, latest)
    if installed.returncode != 0:
        detail = installed.stderr.strip() or installed.stdout.strip()
        rollback = _rollback(pipx, previous)
        raise RuntimeError(f"Budge update failed: {detail}; {rollback}")

    smoke = util.run([budge_bin, "--version"], check=False)
    expected = f"budge {latest.version} ({latest.commit[:12]})"
    if smoke.returncode != 0 or smoke.stdout.strip() != expected:
        detail = smoke.stderr.strip() or smoke.stdout.strip() \
            or "the updated command did not start"
        rollback = _rollback(pipx, previous)
        raise RuntimeError(f"Budge {latest.tag} failed verification: "
                           f"{detail}; {rollback}")

    success(f"updated Budge to {latest.version}")
    if refresh_services:
        _refresh_units(budge_bin)
    _refresh_manpage()
