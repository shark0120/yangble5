"""CI's stdlib-only check, run locally instead of discovered in CI.

``tools/`` is standard-library-only so a user debugging a proxy can copy one
file onto a box and run it.  A dedicated CI job installs nothing and asserts
that: statically, by walking every import with ``ast``, and then at runtime by
importing each module and running each script with ``--help``.

That job is the only thing enforcing the promise, and it lives entirely inside
``.github/workflows/ci.yml`` — so the first time anyone finds out they broke it
is a red build, minutes after the push.  On 2026-07-23 that is exactly what
happened: ``tools/sitecheck.py`` gained ``from drift_check import PUBLISHED``,
which the static walk rejected because ``drift_check`` is not in
``sys.stdlib_module_names``.  It is not a third-party package either — it is
the file sitting next to it in ``tools/`` — but the check could not tell the
difference, and the bare spelling is the only one that resolves under ``python
tools/sitecheck.py``, where ``sys.path[0]`` is ``tools/`` and the repository
root is not on the path at all.

So the rule now allows a bare import of a SIBLING ``.py`` in the importing
file's own directory, and this module lifts the script out of the workflow and
runs it here.  Lifting rather than copying is deliberate and is the same
technique ``scripts/make_history.sh`` uses for the secret-scan pattern: one
authoritative copy is the only kind that cannot drift.  A second copy in this
file would be a second thing to forget.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml is a test-only dependency")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
STEP = "No non-stdlib import anywhere in tools/ or byok/"


def _lift_the_check() -> str:
    """The heredoc body of the workflow step, verbatim.

    Fails loudly rather than silently skipping if the step is renamed: a lifted
    script that quietly stops being found is a test that passes while checking
    nothing, which is the failure this repository keeps finding in its own
    gates.
    """
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in spec["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == STEP:
                run = step["run"]
                assert "<<'PY'" in run, f"the {STEP!r} step no longer uses a PY heredoc"
                return textwrap.dedent(run.split("<<'PY'", 1)[1].rsplit("PY", 1)[0])
    raise AssertionError(
        f"no step named {STEP!r} in {WORKFLOW}. It was renamed or removed, and this "
        "test was about to pass without checking anything."
    )


def _run(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, interpreter is sys.executable
        [sys.executable, "-c", script],
        capture_output=True,
        # NOT bare `text=True`. That decodes with the machine's locale codec,
        # which on a Traditional-Chinese Windows install is cp950 and on a US
        # one is cp1252 -- and `tools/sitecheck.py --help` prints em dashes.
        # The UnicodeDecodeError surfaces on subprocess's internal reader
        # thread, so pytest reports it as PytestUnhandledThreadExceptionWarning
        # rather than as a decode error, which is a long way from the cause.
        # These subprocesses are Python writing UTF-8; say so.
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=cwd,
    )


def test_the_real_tree_passes_the_check_ci_will_run() -> None:
    proc = _run(_lift_the_check(), ROOT)
    assert proc.returncode == 0, (
        "tools/ or byok/ imports something outside the standard library, so the "
        "copy-one-file-and-run promise is broken:\n" + proc.stdout + proc.stderr
    )
    assert "every import is in the standard library" in proc.stdout


def test_a_sibling_module_is_not_a_third_party_package(tmp_path: Path) -> None:
    """The false positive that cost a CI round.

    ``tools/sitecheck.py`` imports ``drift_check`` by bare name because that is
    the only spelling that works when the file is run as a script.
    """
    tree = tmp_path / "repo"
    (tree / "tools").mkdir(parents=True)
    (tree / "byok").mkdir()
    (tree / "tools" / "neighbour.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tree / "tools" / "user.py").write_text("from neighbour import VALUE\n", encoding="utf-8")

    proc = _run(_lift_the_check(), tree)
    assert proc.returncode == 0, (
        "a bare import of a .py file sitting in the same directory was rejected "
        "as third-party:\n" + proc.stdout + proc.stderr
    )


@pytest.mark.parametrize(
    ("where", "source", "needle"),
    [
        # The thing the check is actually for.
        ("tools/leaky.py", "import httpx\n", "httpx"),
        # Buried in a function: static analysis catches what a smoke import cannot.
        ("tools/lazy.py", "def go():\n    import yaml\n    return yaml\n", "yaml"),
        # from-imports too.
        ("byok/leaky.py", "from requests import get\n", "requests"),
        # A sibling name is only allowed from the SAME directory. byok/ may not
        # reach into tools/ by bare name, because that import would not resolve.
        ("byok/reacher.py", "import neighbour\n", "neighbour"),
    ],
)
def test_the_check_still_catches_a_real_violation(
    tmp_path: Path, where: str, source: str, needle: str
) -> None:
    tree = tmp_path / "repo"
    (tree / "tools").mkdir(parents=True)
    (tree / "byok").mkdir()
    (tree / "tools" / "neighbour.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tree / where).write_text(source, encoding="utf-8")

    proc = _run(_lift_the_check(), tree)
    assert proc.returncode != 0, (
        f"{where} imports {needle!r} and the check passed. Widening the sibling "
        "allowance has made it unable to fail.\n" + proc.stdout
    )
    assert needle in proc.stdout


@pytest.mark.skipif(shutil.which(sys.executable) is None, reason="no interpreter")
def test_every_tool_imports_and_answers_help() -> None:
    """The runtime half, which the static walk cannot cover.

    An import that resolves in the repository root but not when the file is run
    as a script — precisely the case that produced the two-spelling import in
    ``tools/sitecheck.py`` — is invisible to ``ast`` and shows up only here.
    """
    for module in ("tools.sitecheck", "tools.drift_check", "byok.setup"):
        proc = _run(f"import {module}", ROOT)
        assert proc.returncode == 0, f"import {module} failed:\n{proc.stderr}"

    for script in ("tools/sitecheck.py", "tools/drift_check.py", "byok/setup.py"):
        proc = subprocess.run(  # noqa: S603 - fixed argv, interpreter is sys.executable
            [sys.executable, str(ROOT / script), "--help"],
            capture_output=True,
            encoding="utf-8",   # see _run: locale decoding breaks on em dashes
            errors="replace",
            timeout=120,
            cwd=ROOT,
        )
        assert proc.returncode == 0, f"{script} --help failed:\n{proc.stderr}"
