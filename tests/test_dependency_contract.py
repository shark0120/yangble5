"""Dependency declarations must describe one gateway runtime contract."""

from __future__ import annotations

import pathlib
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10 CI
    import tomli as tomllib


ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "gateway" / "requirements.txt"
PYPROJECT = ROOT / "pyproject.toml"
RUNTIME_PACKAGES = ("fastapi", "httpx")
LOWER_BOUND = re.compile(r"^([A-Za-z0-9_-]+)>=(\d+(?:\.\d+)*)")


def _lower_bounds(specs: list[str]) -> dict[str, str]:
    bounds = {}
    for spec in specs:
        match = LOWER_BOUND.match(spec)
        if match and match.group(1) in RUNTIME_PACKAGES:
            bounds[match.group(1)] = match.group(2)
    return bounds


def test_gateway_dependency_floors_match_every_install_surface():
    requirements = [
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    with PYPROJECT.open("rb") as handle:
        extras = tomllib.load(handle)["project"]["optional-dependencies"]

    expected = _lower_bounds(requirements)
    assert expected == {"fastapi": "0.140.0", "httpx": "0.28.1"}
    assert _lower_bounds(extras["gateway"]) == expected, (
        "the gateway extra and deployment requirements install the same runtime; "
        "their lower bounds must move together"
    )
    assert _lower_bounds(extras["dev"]) == expected, (
        "the dev suite must test the same gateway dependency floors users install"
    )
