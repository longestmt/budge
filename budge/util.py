"""Small shared helpers: subprocess, prompting, terminal styling, dry-run."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

# Global dry-run flag, set once by the CLI (--dry-run, acceptance A12).
DRY_RUN = False

# ---------------------------------------------------------------- colors ---

_STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32",
           "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36"}


def _isatty(stream) -> bool:
    return (hasattr(stream, "isatty") and stream.isatty()
            and not os.environ.get("NO_COLOR"))


def paint(text: str, *styles: str, stream=None) -> str:
    """ANSI-style text; plain when piped/redirected or NO_COLOR is set."""
    if not _isatty(stream or sys.stdout):
        return text
    codes = ";".join(_STYLES[s] for s in styles if s in _STYLES)
    return f"\033[{codes}m{text}\033[0m"


BANNER = r"""
 _               _
| |__  _   _  __| | __ _  ___
| '_ \| | | |/ _` |/ _` |/ _ \
| |_) | |_| | (_| | (_| |  __/
|_.__/ \__,_|\__,_|\__, |\___|
                   |___/      """


def banner(subtitle: str = "") -> None:
    say(paint(BANNER, "cyan", "bold"))
    if subtitle:
        say(paint(f"   {subtitle}\n", "dim"))


def header(title: str) -> None:
    """A visually distinct section header."""
    rule = "─" * max(4, 62 - len(title))
    say("\n" + paint(f"── {title} {rule}", "magenta", "bold"))


def success(msg: str) -> None:
    say(paint("✓ ", "green", "bold") + msg)


def note(msg: str) -> None:
    say(paint(msg, "dim"))


def say(msg: str) -> None:
    print(msg)


def warn(msg: str) -> None:
    print(paint("warning:", "yellow", "bold", stream=sys.stderr) + f" {msg}",
          file=sys.stderr)


def die(msg: str, code: int = 1) -> "None":
    print(paint("error:", "red", "bold", stream=sys.stderr) + f" {msg}",
          file=sys.stderr)
    raise SystemExit(code)


def dry(action: str) -> bool:
    """If in dry-run mode, print the intended action and return True (skip)."""
    if DRY_RUN:
        print(f"[dry-run] {action}")
        return True
    return False


def run(cmd, cwd=None, input_text=None, check=True, env=None):
    """Run a command, capturing output. Raises on failure when check=True.

    Guarantees a UTF-8 locale for the child: GHC programs like hledger
    refuse to decode UTF-8 journals under a C/POSIX locale (common on
    minimal LXC consoles).
    """
    if env is None:
        env = dict(os.environ)
    if "UTF-8" not in (env.get("LC_ALL") or env.get("LANG") or ""):
        env["LC_ALL"] = "C.UTF-8"
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
    suffix = paint(f" [{default}]", "dim") if default else ""
    try:
        answer = input(paint(question, "cyan", "bold") + suffix
                       + paint(": ", "cyan")).strip()
    except EOFError:
        answer = ""
    return answer or default


def choose(question: str, options: list, default: int = 0):
    """Enumerated selection. `options` is a list of (value, label) pairs.

    The user answers with a number (or blank for the default); returns the
    chosen value.
    """
    say(paint(question, "cyan", "bold"))
    for i, (_, label) in enumerate(options, 1):
        marker = paint("  (default)", "green") if i - 1 == default else ""
        say(f"  {paint(f'{i})', 'bold')} {label}{marker}")
    while True:
        answer = prompt("enter a number", str(default + 1)).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        for value, _ in options:
            if answer == value:
                return value
        say(f"please enter a number between 1 and {len(options)}")


def choose_multi(question: str, options: list, selected=None) -> set:
    """Enumerated MULTI-select. Answer with numbers ('1,3'), '0' for none,
    or blank to keep the current selection. Returns the chosen values."""
    import re as _re
    selected = set(selected or [])
    say(paint(question, "cyan", "bold"))
    say(f"  {paint('0)', 'bold')} none")
    for i, (value, label) in enumerate(options, 1):
        mark = paint("[x]", "green", "bold") if value in selected \
            else "[ ]"
        say(f"  {paint(f'{i})', 'bold')} {mark} {label}")
    while True:
        answer = prompt("numbers, comma-separated (blank keeps current)",
                        "").strip()
        if not answer:
            return selected
        if answer in ("0", "none"):
            return set()
        parts = [p for p in _re.split(r"[,\s]+", answer) if p]
        if all(p.isdigit() and 1 <= int(p) <= len(options) for p in parts):
            return {options[int(p) - 1][0] for p in parts}
        say(f"please enter numbers between 0 and {len(options)}, "
            "comma-separated")


def confirm(question: str, default: bool = False) -> bool:
    hint = paint("(Y/n)" if default else "(y/N)", "dim")
    try:
        answer = input(paint(question, "cyan", "bold")
                       + f" {hint}: ").strip().lower()
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
