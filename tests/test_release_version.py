"""Lock the release checklist's version claim to the files a release ships.

``RELEASING.md`` used to say nothing else hard-coded the project version while
``gateway/__init__.py`` did exactly that and exposed the copy on public
``/health``. A checklist that omits a required bump location creates a release
defect while being followed perfectly. These tests make the documented model
executable: pyproject is authoritative, the copied gateway package needs a
runtime mirror, and the public response example mirrors that runtime value.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI cell
    import tomli as tomllib


ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
GATEWAY_INIT = ROOT / "gateway" / "__init__.py"
GATEWAY_APP = ROOT / "gateway" / "app.py"
GATEWAY_DOCKERFILE = ROOT / "deploy" / "Dockerfile.gateway"
SITE_README = ROOT / "site" / "README.md"
RELEASING = ROOT / "RELEASING.md"


def _project_version() -> str:
    with PYPROJECT.open("rb") as handle:
        value = tomllib.load(handle)["project"]["version"]
    assert isinstance(value, str) and value, "pyproject [project].version is not a non-empty string"
    return value


def _gateway_version() -> str:
    tree = ast.parse(GATEWAY_INIT.read_text(encoding="utf-8"), filename=str(GATEWAY_INIT))
    values = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        is_version = any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
        if is_version:
            values.append(node.value.value if isinstance(node.value, ast.Constant) else None)
    assert len(values) == 1, (
        f"expected one literal __version__ assignment in {GATEWAY_INIT}, found {len(values)}. "
        "The copied gateway package must carry the release version without installed metadata."
    )
    assert isinstance(values[0], str) and values[0], (
        "gateway.__version__ must remain a non-empty string literal so the copy-only container "
        "has a version before any package metadata exists"
    )
    return values[0]


def _documented_health_version() -> str:
    text = SITE_README.read_text(encoding="utf-8")
    heading = "## The `/api/health` contract"
    assert text.count(heading) == 1, f"expected one {heading!r} section in {SITE_README}"
    section = text.split(heading, 1)[1]
    match = re.search(r"```json\s*\n(.*?)\n```", section, re.DOTALL)
    assert match is not None, f"{SITE_README} no longer shows the health response as JSON"
    payload = json.loads(match.group(1))
    value = payload.get("version")
    assert isinstance(value, str) and value, "the documented public health response has no version"
    return value


def _version_section() -> str:
    text = RELEASING.read_text(encoding="utf-8")
    heading = "## 2. Version bump locations"
    assert text.count(heading) == 1, f"expected one {heading!r} section in {RELEASING}"
    return text.split(heading, 1)[1].split("\n---", 1)[0]


def test_current_version_source_and_mirrors_agree():
    authoritative = _project_version()
    assert _gateway_version() == authoritative, (
        "gateway/__init__.py disagrees with pyproject.toml; /health would report a stale release"
    )
    assert _documented_health_version() == authoritative, (
        "site/README.md's public health example disagrees with pyproject.toml"
    )


def test_release_checklist_names_the_source_and_both_mirrors():
    section = _version_section()
    for path in ("pyproject.toml", "gateway/__init__.py", "site/README.md"):
        assert f"`{path}`" in section, (
            f"{RELEASING} omits the required version location {path}; a releaser following the "
            "checklist would leave a stale public version behind"
        )
    assert "authoritative source" in section
    assert "`/health` and `/healthz` responses deliberately expose" in section
    assert "not the version of the internal engine" in section


def test_gateway_container_really_needs_the_documented_runtime_mirror():
    dockerfile = GATEWAY_DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY gateway/ /app/gateway/" in dockerfile
    assert "pyproject.toml" not in dockerfile, (
        "the gateway image now contains project metadata. Re-evaluate deriving __version__ from "
        "pyproject instead of keeping the runtime mirror, then update RELEASING.md and this test"
    )


def test_public_health_response_is_wired_to_the_runtime_version():
    source = GATEWAY_APP.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(GATEWAY_APP))
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "health"
    ]
    assert len(handlers) == 1, (
        f"expected one health handler in {GATEWAY_APP}, found {len(handlers)}"
    )
    handler = handlers[0]
    paths = {
        decorator.args[0].value
        for decorator in handler.decorator_list
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "get"
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[0].value, str)
        )
    }
    assert {"/health", "/healthz"} <= paths, (
        f"the documented public version routes are not both attached to the health handler: {paths}"
    )
    handler_source = ast.get_source_segment(source, handler)
    assert handler_source is not None and '"version": _package_version()' in handler_source, (
        "RELEASING.md says public health exposes the project version, but the health handler no "
        "longer builds its response from the runtime version"
    )
