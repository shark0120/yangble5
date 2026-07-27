"""Make an unclassified workflow step fail loudly.

The gate roster in ``test_contributing_gates.py`` deliberately recognizes
specific executable shapes.  That precision is useful, but it also means a
wholly new shape could be invisible to the roster.  GitHub Actions has no gate
type annotation to derive, so this file uses the smallest honest tripwire:
each job's total step count and named step count.

The inventory is intentionally about counts, not step names.  Renaming
``Install`` does not change what CI does and should not make a pull request red.
Adding a step does change what CI does; the failure tells the author to put a
real content gate in CONTRIBUTING's roster, or update the inventory and explain
why a non-gate support step was added.

Both total and named counts matter.  Counting only ``- name:`` entries would
miss an unnamed ``uses:`` step, while counting only totals would let a named
check silently become anonymous.  A new job is also a failure rather than an
uninspected island.
"""

from __future__ import annotations

import os
import pathlib

import yaml

DEFAULT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT = pathlib.Path(os.environ.get("YB5_CI_STEP_INVENTORY_ROOT", DEFAULT_ROOT))
CI = ROOT / ".github" / "workflows" / "ci.yml"

# job: (all steps, steps with a human-readable name)
EXPECTED_STEP_COUNTS = {
    "test": (6, 5),
    "cache-guard-action": (4, 3),
    "tools-are-stdlib-only": (6, 4),
    "offline-self-checks": (13, 11),
    "installer-digests": (2, 1),
    "published-numbers": (8, 7),
    "no-secrets": (4, 3),
    "live-site-drift": (4, 2),
}


def _jobs() -> dict:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), f"{CI} did not parse as a YAML mapping"
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{CI} has no non-empty jobs mapping"
    return jobs


def test_every_ci_job_is_in_the_step_inventory():
    actual = set(_jobs())
    expected = set(EXPECTED_STEP_COUNTS)
    assert actual == expected, (
        "ci.yml's job set changed without an inventory decision.\n"
        f"  added: {sorted(actual - expected)}\n"
        f"  removed: {sorted(expected - actual)}\n\n"
        "If an added job contains repository gates, document them in CONTRIBUTING. "
        "Then update EXPECTED_STEP_COUNTS and explain the new job in the change."
    )


def test_every_ci_job_has_the_expected_total_and_named_step_count():
    jobs = _jobs()
    problems = []
    for job, expected in EXPECTED_STEP_COUNTS.items():
        if job not in jobs:
            problems.append(f"{job}: job is missing")
            continue
        steps = jobs[job].get("steps")
        assert isinstance(steps, list) and steps, f"job {job!r} has no non-empty steps list"
        actual = (len(steps), sum(isinstance(step, dict) and "name" in step for step in steps))
        if actual != expected:
            problems.append(
                f"{job}: expected total/named={expected[0]}/{expected[1]}, "
                f"found {actual[0]}/{actual[1]}"
            )
    assert problems == [], (
        "A CI job grew or lost a step that the gate roster cannot classify:\n  "
        + "\n  ".join(problems)
        + "\n\nIf it is a gate, add it to CONTRIBUTING and its executable signatures "
        "to test_contributing_gates.py. If it is setup/reporting only, update this "
        "inventory and explain why in the change."
    )


def test_each_inventoried_step_has_one_executable_shape():
    """The count must describe executable steps, not malformed YAML mappings."""
    for job, config in _jobs().items():
        steps = config.get("steps")
        assert isinstance(steps, list) and steps, f"job {job!r} has no non-empty steps list"
        for index, step in enumerate(steps, 1):
            assert isinstance(step, dict), f"job {job!r} step {index} is not a mapping"
            shapes = [key for key in ("run", "uses") if key in step]
            assert len(shapes) == 1, (
                f"job {job!r} step {index} has executable shapes {shapes}; "
                "each step must have exactly one of `run` or `uses`"
            )


def test_pytest_matrix_still_expands_to_ten_supported_combinations():
    """An action bump must not silently narrow the advertised Python matrix."""
    matrix = _jobs()["test"]["strategy"]["matrix"]
    assert matrix["os"] == ["ubuntu-latest", "windows-latest"]
    assert matrix["python-version"] == ["3.10", "3.11", "3.12", "3.13", "3.14"]
    assert len(matrix["os"]) * len(matrix["python-version"]) == 10
