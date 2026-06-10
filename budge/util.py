"""Small shared helpers: subprocess, prompting, dry-run, $EDITOR."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

# Global dry-run flag, set once by the CLI (--dry-run, acceptance A12).
DRY_RUN = False


def say(msg: str) -> None:
    print(msg)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def dry(action: str) -> bool:
    """If in dry-run mode, print the intended action and return True (skip)."""
    if DRY_RUN:
        print(f"[dry-run] {action}")
        return True
    return False


def run(cmd, cwd=None, input_text=None, check=True, env=None):
    """Run a command, capturing output. Raises on failure when check=True."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(map(str, cmd))}\n"
            f"stdout: {proc.stdout.strip()}\nstderr: {proc.stderr.strip()}"
        )
    return proc


def prompt(question: str, default: str = "") -> str:
    """Prompt for a line of input; supports non-interactive scripted answers."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def confirm(question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} ({hint}): ").strip().lower()
    except EOFError:
        answer = ""
    if not answer:
        return default
    return answer in ("y", "yes")


def edit_text(text: str, suffix: str = ".txt") -> str:
    """Open text in $EDITOR and return the edited result."""
    editor = os.environ.get("EDITOR", "nano")
    with tempfile.NamedTemporaryFile(
        "w+", suffix=suffix, delete=False, encoding="utf-8"
    ) as tf:
        tf.write(text)
        path = tf.name
    try:
        subprocess.run([editor, path], check=True)
        with open(path, encoding="utf-8") as f:
            return f.read()
    finally:
        os.unlink(path)


def append_file(path, text: str) -> None:
    if dry(f"append {len(text)} chars to {path}"):
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def write_file(path, text: str, mode: int = None) -> None:
    if dry(f"write {len(text)} chars to {path}"):
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)
