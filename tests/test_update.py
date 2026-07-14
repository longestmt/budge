"""Release discovery and safe self-update behavior."""

from types import SimpleNamespace

import pytest

from budge import update


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout,
                           stderr=stderr)


def releases(*versions):
    return [update.Release(
        version=version,
        version_key=tuple(int(n) for n in version.split(".")),
        tag=f"v{version}",
        commit=(str(i) * 40),
    ) for i, version in enumerate(versions, 1)]


def test_parse_releases_prefers_peeled_tag_and_sorts_versions():
    parsed = update._parse_releases(
        "a" * 40 + "\trefs/tags/v1.10.0\n"
        + "b" * 40 + "\trefs/tags/v1.2.0\n"
        + "c" * 40 + "\trefs/tags/v1.2.0^{}\n"
        + "d" * 40 + "\trefs/tags/v2.0.0-rc1\n"
        + "e" * 40 + "\trefs/tags/not-a-release\n"
    )
    assert [item.version for item in parsed] == ["1.2.0", "1.10.0"]
    assert parsed[0].commit == "c" * 40


def test_check_reports_update_without_installing(monkeypatch, capsys):
    monkeypatch.setattr(update, "__version__", "1.1.0")
    monkeypatch.setattr(update, "available_releases",
                        lambda: releases("1.1.0", "1.2.0"))
    monkeypatch.setattr(update, "_install",
                        lambda *args: pytest.fail("check must not install"))
    update.run_update(check_only=True)
    out = capsys.readouterr().out
    assert "installed: 1.1.0" in out
    assert "latest stable: 1.2.0" in out
    assert "update available: 1.1.0 -> 1.2.0" in out


def test_update_installs_resolved_commit_and_verifies(monkeypatch, capsys):
    found = releases("1.1.0", "1.2.0")
    calls = []
    monkeypatch.setattr(update, "__version__", "1.1.0")
    monkeypatch.setattr(update, "available_releases", lambda: found)
    monkeypatch.setattr(update.shutil, "which",
                        lambda name: f"/fake/{name}")
    monkeypatch.setattr(update, "_refresh_manpage", lambda: None)

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)
        if cmd == ["/fake/budge", "--version"]:
            return result(stdout="budge 1.2.0 (" + "2" * 12 + ")\n")
        return result()

    monkeypatch.setattr(update.util, "run", fake_run)
    update.run_update(refresh_services=False)
    assert calls[0] == [
        "/fake/pipx", "install", "--force",
        "git+https://github.com/longestmt/budge.git@" + "2" * 40,
    ]
    assert calls[1] == ["/fake/budge", "--version"]
    assert "updated Budge to 1.2.0" in capsys.readouterr().out


def test_failed_verification_rolls_back_to_previous_release(monkeypatch):
    found = releases("1.1.0", "1.2.0")
    installs = []
    monkeypatch.setattr(update, "__version__", "1.1.0")
    monkeypatch.setattr(update, "available_releases", lambda: found)
    monkeypatch.setattr(update.shutil, "which",
                        lambda name: f"/fake/{name}")
    monkeypatch.setattr(update, "_refresh_manpage", lambda: None)

    def fake_run(cmd, check=False, **kwargs):
        if cmd[:3] == ["/fake/pipx", "install", "--force"]:
            installs.append(cmd[-1])
            return result()
        return result(returncode=1, stderr="cannot import budge")

    monkeypatch.setattr(update.util, "run", fake_run)
    with pytest.raises(RuntimeError, match="restored v1.1.0"):
        update.run_update(refresh_services=False)
    assert installs == [
        "git+https://github.com/longestmt/budge.git@" + "2" * 40,
        "git+https://github.com/longestmt/budge.git@" + "1" * 40,
    ]


def test_dry_run_never_installs(monkeypatch, capsys):
    monkeypatch.setattr(update, "__version__", "1.1.0")
    monkeypatch.setattr(update, "available_releases",
                        lambda: releases("1.2.0"))
    monkeypatch.setattr(update, "_install",
                        lambda *args: pytest.fail("dry-run must not install"))
    update.util.DRY_RUN = True
    try:
        update.run_update()
    finally:
        update.util.DRY_RUN = False
    assert "[dry-run] install Budge v1.2.0" in capsys.readouterr().out


def test_no_published_release_is_a_clean_noop(monkeypatch, capsys):
    monkeypatch.setattr(update, "available_releases", lambda: [])
    update.run_update()
    assert "No stable Budge releases" in capsys.readouterr().out


def test_update_unit_refresh_does_not_touch_books_repo(env, monkeypatch):
    from budge import setup_cmd
    from budge.gitutil import git

    before = git(env.repo, "status", "--porcelain").stdout
    monkeypatch.setattr(setup_cmd, "_install_units",
                        lambda cfg, rendered: None)
    setup_cmd.run_setup(env.cfg, units_only=True)
    after = git(env.repo, "status", "--porcelain").stdout

    assert after == before == ""
    rendered = env.confdir / "systemd"
    assert (rendered / "budge-fetch.service").exists()
