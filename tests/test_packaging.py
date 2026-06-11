"""Packaging regressions: the unit templates must ship inside the package
(a pipx install has no access to the source checkout's top level)."""

from pathlib import Path

import budge
from budge.setup_cmd import _render_units


def test_unit_templates_live_inside_the_package():
    src = Path(budge.__file__).resolve().parent / "systemd"
    names = {p.name for p in src.glob("*.in")}
    assert "budge-fetch.service.in" in names
    assert "paisa.service.in" in names
    assert len(names) >= 11


def test_render_units_substitutes_placeholders(env):
    rendered = _render_units(env.cfg)
    fetch_service = (rendered / "budge-fetch.service").read_text()
    assert "@BUDGE@" not in fetch_service
    assert "@CONFIG_DIR@" not in fetch_service
    assert str(env.confdir) in fetch_service
    timer = (rendered / "budge-fetch.timer").read_text()
    assert "@SCHEDULE_FETCH@" not in timer
    paisa = (rendered / "paisa.service").read_text()
    assert str(env.repo) in paisa
