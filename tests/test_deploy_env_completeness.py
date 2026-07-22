"""Every secret the gateway reads must be reachable from the files an operator edits.

On 2026-07-23 ``BYOK_ENCRYPTION_KEY`` appeared in ``gateway/`` and in ``tests/``
and **nowhere else**: not in ``deploy/.env.example``, not in either compose
file, not in a document, and in no check.  ``BYOK_ENABLED`` defaults to
``True``.  So a self-hoster who followed the documented deploy ended up
accepting other people's upstream provider credentials — their Google, OpenAI
or xAI keys, the ones that bill *their* accounts — and storing them **as-is, in
plaintext**, in a SQLite file.

The gateway does append a startup warning.  That is one line in a log, on a box
the operator has just finished setting up, against a default that is on.  It is
not the same thing as the variable being present in the file they fill in.

What makes this worth a test rather than a one-time fix: the failure was
*silence*.  Nothing was wrong, nothing errored, and the only signal was the
absence of a line nobody knew to look for.  A variable that the gateway treats
as security-relevant and that no operator-facing file mentions is the shape of
that failure, so that is what is asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml is a test-only dependency")

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
COMPOSE = [
    ROOT / "deploy" / "docker-compose.yml",
    ROOT / "deploy" / "docker-compose.behind-proxy.yml",
]

# Settings the gateway will happily run without, where running without them has
# a security consequence the operator would not choose on purpose. Each must be
# present in .env.example AND in every compose file.
#
# Deliberately a short, hand-curated list rather than everything Settings reads:
# most variables are tuning knobs whose absence is harmless, and a test that
# demanded all of them would be noise that gets suppressed.
SECURITY_RELEVANT = {
    "BYOK_ENCRYPTION_KEY": (
        "without it, a user's own upstream provider credential is stored in "
        "plaintext in the operator's database"
    ),
    "KEY_PEPPER": (
        "without it, a stolen database needs no second secret to attack the "
        "stored key hashes"
    ),
    "ADMIN_API_KEY": (
        "without it, invite mode cannot mint an invite and nobody can register"
    ),
}


def _env_example_names() -> set[str]:
    """Assignments in .env.example, with the YANGBLE5_ prefix stripped.

    Commented-out lines count: `# YANGBLE5_PRICE_TABLE_FILE=...` is how this
    file documents an optional setting, and being documented is the property
    under test, not being uncommented.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    names = set(re.findall(r"(?m)^\s*#?\s*YANGBLE5_([A-Z0-9_]+)\s*=", text))
    return names


def _compose_names(path: Path) -> set[str]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    env = spec["services"]["gateway"]["environment"]
    keys = env.keys() if isinstance(env, dict) else [e.split("=", 1)[0] for e in env]
    return {k[len("YANGBLE5_") :] for k in keys if k.startswith("YANGBLE5_")}


@pytest.mark.parametrize("name", sorted(SECURITY_RELEVANT))
def test_env_example_documents_the_security_relevant_settings(name: str) -> None:
    assert name in _env_example_names(), (
        f"deploy/.env.example never mentions YANGBLE5_{name}. "
        f"{SECURITY_RELEVANT[name]}.\n\n"
        "An operator fills in this file; a setting that is not in it is a "
        "setting they cannot know exists. That is exactly how "
        "BYOK_ENCRYPTION_KEY went missing while BYOK defaulted to on."
    )


@pytest.mark.parametrize("path", COMPOSE, ids=lambda p: p.name)
@pytest.mark.parametrize("name", sorted(SECURITY_RELEVANT))
def test_every_compose_file_passes_them_through(path: Path, name: str) -> None:
    assert name in _compose_names(path), (
        f"{path.name} does not pass YANGBLE5_{name} to the gateway, so a value "
        f"set in .env never reaches the process. {SECURITY_RELEVANT[name]}."
    )


@pytest.mark.parametrize("path", COMPOSE, ids=lambda p: p.name)
def test_no_compose_variable_is_missing_from_env_example(path: Path) -> None:
    """The reverse direction, which is how the next one gets lost.

    A variable wired up in compose but absent from ``.env.example`` looks
    configured and is not: ``${VAR:-default}`` silently takes the default, and
    the operator has no line to edit.
    """
    documented = _env_example_names()
    missing = sorted(n for n in _compose_names(path) if n not in documented)
    assert not missing, (
        f"{path.name} passes these to the gateway, but deploy/.env.example "
        f"documents none of them:\n  " + "\n  ".join(missing) + "\n\n"
        "Either add each to .env.example (commented out is fine — that is how "
        "the file marks an optional setting) or stop passing it."
    )


def test_the_byok_encryption_key_is_required_not_defaulted() -> None:
    """BYOK is on by default, so an unset key must stop the deploy.

    ``${VAR:-}`` would let compose start a gateway that accepts other people's
    provider credentials and stores them in the clear. ``${VAR:?message}`` makes
    it refuse. The operator who genuinely does not want to hold those
    credentials has a documented alternative — ``YANGBLE5_BYOK_ENABLED=false``
    — and the error message names it.
    """
    for path in COMPOSE:
        raw = path.read_text(encoding="utf-8")
        line = next(
            (ln for ln in raw.splitlines() if "YANGBLE5_BYOK_ENCRYPTION_KEY:" in ln), ""
        )
        assert ":?" in line, (
            f"{path.name} gives YANGBLE5_BYOK_ENCRYPTION_KEY a default instead of "
            f"requiring it:\n  {line.strip()}\n"
            "BYOK defaults to ON. A default here means an operator who never "
            "read about it silently stores their users' provider keys in "
            "plaintext, which is the failure this file exists for."
        )
        assert "BYOK_ENABLED=false" in line, (
            f"{path.name}'s error message does not tell the operator the way out. "
            "Refusing to start without offering the alternative just gets the "
            "variable set to a dummy value."
        )
