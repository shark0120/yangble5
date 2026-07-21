"""Tests for the offline-testable logic in byok/setup.py.

Nothing here touches the network, spawns the engine, or reads a real credential.
What it does hold in place is the set of properties that decide whether a BYOK
install caches or not:

* the template renders to VALID YAML with the cache-preserving settings present;
* an alias can never be rendered into a same-alias multi-model pool, which is the
  exact shape that makes CLIProxyAPI rotate upstreams per request and caps the
  hit rate near 1/N;
* client config writers back up whatever was there and never clobber it;
* input validation rejects the values that would otherwise inject YAML, or reach
  the engine as an opaque 401 forty seconds later.
"""

from __future__ import annotations

import json
import tomllib
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from byok import setup as byok

TEMPLATE = (Path(byok.__file__).resolve().parent / "config.template.yaml").read_text(
    encoding="utf-8"
)
NOW = datetime(2026, 7, 21, 16, 30, 0)


def make_spec(**overrides) -> byok.Spec:
    """A spec that is complete enough to render, with a fake-but-plausible key."""
    base = {
        "provider_kind": byok.PROVIDER_GEMINI,
        "alias": "yangble5",
        "model": "gemini-2.5-pro",
        "upstream_key": "AIzaSyFAKEfakeFAKEfake0123456789abcdef",
        "local_key": "yb5_local_deadbeefdeadbeefdeadbeef",
        # A fixed, obviously-fake POSIX path rather than tmp_path: these tests
        # assert on RENDERED TEXT, and a per-run temporary directory would make
        # the expected auth-dir string different on every run and on every OS.
        # Nothing here is created on disk; the tests that touch the filesystem
        # take tmp_path instead.
        "out_dir": Path("/home/tester/.yangble5/byok"),
        "auth_dir": Path("/home/tester/.yangble5/byok/auth"),
    }
    base.update(overrides)
    return byok.Spec(**base)


def render(spec: byok.Spec) -> dict:
    """Render the engine config for ``spec`` and parse it back as YAML."""
    return yaml.safe_load(byok.build_config(TEMPLATE, spec))


# ----------------------------------------------------------- template render ---


def test_template_ships_with_the_placeholders_setup_expects():
    """A template edit that renames a token must fail here, not in the field."""
    found = set(byok.PLACEHOLDER_RE.findall(TEMPLATE))
    assert found == {
        "ENGINE_HOST",
        "ENGINE_PORT",
        "AUTH_DIR",
        "LOCAL_API_KEY",
        "SESSION_AFFINITY_TTL",
        "PROVIDER_BLOCK",
    }


def test_template_itself_contains_no_credential():
    """The template is committed; a secret in it would be a secret in git."""
    lowered = TEMPLATE.lower()
    assert "aizasy" not in lowered
    assert "sk-" not in lowered
    assert "yb5_local_" not in lowered


def test_rendered_config_is_valid_yaml_and_leaves_no_placeholder():
    text = byok.build_config(TEMPLATE, make_spec())
    assert byok.PLACEHOLDER_RE.search(text) is None
    assert isinstance(yaml.safe_load(text), dict)


@pytest.mark.parametrize(
    "kind,extra",
    [
        (byok.PROVIDER_GEMINI, {}),
        (
            byok.PROVIDER_OPENAI_COMPAT,
            {"upstream_base_url": "https://api.example.com/v1", "model": "some-model"},
        ),
        (byok.PROVIDER_OAUTH, {"channel": "antigravity", "model": "gemini-pro-agent"}),
    ],
)
def test_every_provider_kind_renders_valid_yaml(kind, extra):
    config = render(make_spec(provider_kind=kind, **extra))
    assert isinstance(config, dict)


def test_cache_preserving_settings_are_present_in_the_rendered_yaml():
    """The three settings this project exists to get right, asserted on the OUTPUT.

    Asserting on the rendered YAML rather than on the template text is the point:
    a future refactor that moves these into the provider block, or comments one
    out, has to fail here.
    """
    config = render(make_spec())
    assert config["routing"]["strategy"] == "fill-first"
    assert config["routing"]["session-affinity"] is True
    assert config["routing"]["session-affinity-ttl"] == "12h"


def test_rendered_config_binds_loopback_only():
    """An engine with no per-user accounting must not be reachable off-host."""
    config = render(make_spec())
    assert config["host"] == "127.0.0.1"
    assert config["port"] == 8318


def test_rendered_config_disables_the_management_api_and_debug_logging():
    config = render(make_spec())
    assert config["remote-management"]["secret-key"] == ""
    assert config["remote-management"]["allow-remote"] is False
    assert config["debug"] is False


def test_rendered_config_carries_the_local_key_and_the_auth_dir():
    spec = make_spec()
    config = render(spec)
    assert config["api-keys"] == [spec.local_key]
    assert config["auth-dir"] == spec.auth_dir.as_posix()


def test_session_ttl_is_rendered_as_a_string_not_a_number():
    """`12h` unquoted is fine, but `3600` unquoted would parse as an int and the
    engine expects a Go duration string."""
    config = render(make_spec(session_ttl="3600s"))
    assert config["routing"]["session-affinity-ttl"] == "3600s"
    assert isinstance(config["routing"]["session-affinity-ttl"], str)


def test_build_config_refuses_a_spec_with_no_local_key():
    with pytest.raises(byok.SetupError, match="local_key"):
        byok.build_config(TEMPLATE, make_spec(local_key=""))


# --------------------------------------------------------- render_template ---


def test_render_template_rejects_a_placeholder_with_no_value():
    with pytest.raises(byok.TemplateError, match="MISSING"):
        byok.render_template("a: __MISSING__", {})


def test_render_template_rejects_a_value_with_no_placeholder():
    """Catches the rename that would otherwise silently drop a setting."""
    with pytest.raises(byok.TemplateError, match="EXTRA"):
        byok.render_template("a: __KNOWN__", {"KNOWN": "1", "EXTRA": "2"})


def test_render_template_substitutes_in_a_single_pass():
    """A replacement value that looks like a placeholder must be inserted
    literally; re-expanding it would let a provider block rewrite the template."""
    out = byok.render_template("k: __A__", {"A": "__B__"})
    assert out == "k: __B__"


def test_render_template_handles_a_repeated_placeholder():
    assert byok.render_template("__X__ __X__", {"X": "v"}) == "v v"


# ------------------------------------------------------- THE alias-pool guard ---


def test_alias_entries_is_always_exactly_one_entry():
    entries = byok.alias_entries(make_spec())
    assert len(entries) == 1
    assert entries[0] == {"name": "gemini-2.5-pro", "alias": "yangble5"}


def test_validate_alias_entries_accepts_distinct_aliases():
    byok.validate_alias_entries(
        [{"name": "model-a", "alias": "one"}, {"name": "model-b", "alias": "two"}]
    )


def test_same_alias_two_models_is_rejected():
    """The whole finding, as an assertion.

    In CLIProxyAPI 7.1.23 this shape is a documented feature (an internal model
    pool) whose upstream is chosen by a per-process counter that ignores
    routing.strategy and session-affinity -- so the prompt cache, which lives
    upstream, is missed on alternating turns.
    """
    with pytest.raises(byok.AliasPoolError) as excinfo:
        byok.validate_alias_entries(
            [
                {"name": "deepseek-v3.1", "alias": "yangble5"},
                {"name": "glm-5", "alias": "yangble5"},
            ]
        )
    message = str(excinfo.value)
    assert "rotating model pool" in message
    # Naming both colliding upstreams is what makes the error actionable.
    assert "deepseek-v3.1" in message and "glm-5" in message


def test_same_alias_repeated_with_the_same_model_is_also_rejected():
    """A duplicated identical entry still builds a pool; it is not a harmless typo."""
    with pytest.raises(byok.AliasPoolError):
        byok.validate_alias_entries(
            [{"name": "grok-4.3", "alias": "grok"}, {"name": "grok-4.3", "alias": "grok"}]
        )


def test_incomplete_alias_entry_is_rejected():
    with pytest.raises(byok.AliasPoolError):
        byok.validate_alias_entries([{"alias": "yangble5"}])


@pytest.mark.parametrize(
    "kind,extra,path",
    [
        (byok.PROVIDER_GEMINI, {}, ("gemini-api-key", 0, "models")),
        (
            byok.PROVIDER_OPENAI_COMPAT,
            {"upstream_base_url": "https://api.example.com/v1"},
            ("openai-compatibility", 0, "models"),
        ),
    ],
)
def test_api_key_providers_emit_exactly_one_model_per_alias(kind, extra, path):
    """Read the alias list back OUT of the rendered YAML and count it."""
    config = render(make_spec(provider_kind=kind, **extra))
    models = config[path[0]][path[1]][path[2]]
    aliases = [entry["alias"] for entry in models]
    assert len(aliases) == len(set(aliases)) == 1


def test_oauth_provider_emits_exactly_one_model_per_alias():
    config = render(make_spec(provider_kind=byok.PROVIDER_OAUTH, model="gemini-pro-agent"))
    entries = config["oauth-model-alias"]["antigravity"]
    aliases = [entry["alias"] for entry in entries]
    assert len(aliases) == len(set(aliases)) == 1
    assert entries[0]["fork"] is True


def test_api_key_providers_do_not_use_oauth_model_alias():
    """oauth-model-alias does not apply to gemini-api-key / openai-compatibility
    entries in 7.1.23. Putting the alias there yields a config that loads fine
    and then 404s the model."""
    config = render(make_spec(provider_kind=byok.PROVIDER_GEMINI))
    assert "oauth-model-alias" not in config
    assert config["gemini-api-key"][0]["models"][0]["alias"] == "yangble5"


def test_oauth_provider_writes_no_credential_into_the_config():
    """OAuth credentials live in auth-dir as token files, never in config.yaml."""
    text = byok.build_config(
        TEMPLATE, make_spec(provider_kind=byok.PROVIDER_OAUTH, upstream_key="")
    )
    config = yaml.safe_load(text)
    assert "gemini-api-key" not in config
    assert "openai-compatibility" not in config


def test_unknown_provider_kind_is_rejected():
    with pytest.raises(byok.SetupError, match="unknown provider kind"):
        byok.render_provider_block(make_spec(provider_kind="carrier-pigeon"))


# --------------------------------------------------------------- YAML safety ---


def test_yaml_quote_escapes_quotes_and_backslashes():
    assert byok.yaml_quote('a"b') == '"a\\"b"'
    assert byok.yaml_quote("a\\b") == '"a\\\\b"'


def test_yaml_quote_refuses_control_characters():
    with pytest.raises(ValueError, match="control characters"):
        byok.yaml_quote("a\nb")


def test_a_key_containing_quotes_and_backslashes_survives_a_yaml_round_trip():
    """Real API keys are opaque. Quoting has to be correct, not merely plausible."""
    hostile = 'abc"def\\ghi'
    config = render(make_spec(upstream_key=hostile))
    assert config["gemini-api-key"][0]["api-key"] == hostile


def test_a_newline_in_an_alias_cannot_inject_config():
    """Defence in depth: the validator rejects it, and yaml_quote would too."""
    with pytest.raises(byok.SetupError):
        byok.validate_alias('yangble5"\nhost: 0.0.0.0')


def test_toml_quote_escapes_and_refuses_control_characters():
    assert byok.toml_quote('a"b') == '"a\\"b"'
    with pytest.raises(ValueError):
        byok.toml_quote("a\tb")


def test_shell_quoting_helpers_neutralise_their_metacharacters():
    assert byok.sh_quote("a'b") == "'a'\\''b'"
    assert byok.sh_quote("$(rm -rf /)") == "'$(rm -rf /)'"
    assert byok.ps_quote("a'b") == "'a''b'"


# --------------------------------------------------------------- validation ---


@pytest.mark.parametrize("value", ["yangble5", "yangble5[1m]", "a", "A.b_c-d"])
def test_validate_alias_accepts_realistic_aliases(value):
    assert byok.validate_alias(value) == value


@pytest.mark.parametrize(
    "value", ["", "   ", "has space", 'has"quote', "has\nnewline", "-leading", "x" * 65]
)
def test_validate_alias_rejects_dangerous_or_malformed_values(value):
    with pytest.raises(byok.SetupError):
        byok.validate_alias(value)


@pytest.mark.parametrize(
    "value", ["gemini-2.5-pro", "moonshotai/kimi-k2:free", "gpt-4o-mini", "gemini-pro-agent"]
)
def test_validate_model_name_accepts_real_upstream_names(value):
    assert byok.validate_model_name(value) == value


@pytest.mark.parametrize("value", ["", "two words", "bad\nname", "#comment"])
def test_validate_model_name_rejects_malformed_values(value):
    with pytest.raises(byok.SetupError):
        byok.validate_model_name(value)


@pytest.mark.parametrize("value,expected", [(8318, 8318), ("8318", 8318), (" 443 ", 443)])
def test_validate_port_accepts_ints_and_strings(value, expected):
    assert byok.validate_port(value) == expected


@pytest.mark.parametrize("value", [0, -1, 65536, "abc", ""])
def test_validate_port_rejects_out_of_range_and_nonsense(value):
    with pytest.raises(byok.SetupError):
        byok.validate_port(value)


@pytest.mark.parametrize("value", ["12h", "30m", "3600s", "500ms"])
def test_validate_ttl_accepts_go_durations(value):
    assert byok.validate_ttl(value) == value


@pytest.mark.parametrize("value", ["", "12 h", "forever", "12", "1d"])
def test_validate_ttl_rejects_anything_the_engine_would_not_parse(value):
    with pytest.raises(byok.SetupError):
        byok.validate_ttl(value)


def test_validate_base_url_strips_a_trailing_slash():
    assert byok.validate_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"


@pytest.mark.parametrize(
    "value", ["", "api.example.com", "ftp://example.com", "https://ex ample.com"]
)
def test_validate_base_url_rejects_non_http_and_malformed(value):
    with pytest.raises(byok.SetupError):
        byok.validate_base_url(value)


def test_plaintext_http_to_a_remote_host_warns_about_the_key_in_the_clear():
    assert byok.base_url_warnings("http://api.example.com/v1")


@pytest.mark.parametrize(
    "value", ["http://127.0.0.1:8080/v1", "http://localhost:1234", "https://api.example.com/v1"]
)
def test_loopback_and_tls_base_urls_do_not_warn(value):
    assert byok.base_url_warnings(value) == []


@pytest.mark.parametrize(
    "value",
    ["AIzaSyFAKEfakeFAKEfake0123456789abcdef", "sk-or-v1-0123456789abcdef0123456789"],
)
def test_validate_upstream_key_accepts_realistic_keys(value):
    assert byok.validate_upstream_key(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "short",
        "has whitespace inside",
        "<your-key-here>",
        "your-api-key-goes-here",
        "sk-or-v1-...b780",
        "CHANGEME_please_1234",
    ],
)
def test_validate_upstream_key_rejects_placeholders_and_typos(value):
    with pytest.raises(byok.SetupError):
        byok.validate_upstream_key(value)


def test_validate_upstream_key_strips_a_trailing_paste_newline():
    assert byok.validate_upstream_key("AIzaSyFAKEfake0123456789abcdef\n").endswith("abcdef")


def test_validate_channel_accepts_known_channels_and_rejects_invented_ones():
    assert byok.validate_channel("antigravity") == "antigravity"
    assert byok.validate_channel(" XAI ") == "xai"
    with pytest.raises(byok.SetupError, match="unknown OAuth channel"):
        byok.validate_channel("openai")


def test_generate_local_key_is_prefixed_and_unique():
    first, second = byok.generate_local_key(), byok.generate_local_key()
    assert first.startswith("yb5_local_")
    assert first != second
    assert len(first) > 30


def test_obtain_upstream_key_reads_the_environment():
    key = "AIzaSyFAKEfakeFAKEfake0123456789abcdef"
    assert byok.obtain_upstream_key("SOME_VAR", False, {"SOME_VAR": key}) == key


def test_obtain_upstream_key_refuses_to_invent_one_when_unattended():
    with pytest.raises(byok.SetupError, match="argv"):
        byok.obtain_upstream_key("SOME_VAR", False, {})


# ---------------------------------------------------------- client configs ---


def test_claude_settings_carry_the_context_unlock_and_no_secret():
    spec = make_spec()
    settings = byok.render_claude_settings(spec)
    assert settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"
    assert settings["env"]["ANTHROPIC_MODEL"] == "yangble5"
    assert spec.local_key not in json.dumps(settings)


def test_claude_settings_point_at_the_shim_by_default_and_the_engine_with_no_shim():
    assert byok.render_claude_settings(make_spec())["env"]["ANTHROPIC_BASE_URL"].endswith(":8320")
    direct = byok.render_claude_settings(make_spec(shim_port=None))
    assert direct["env"]["ANTHROPIC_BASE_URL"].endswith(":8318")


def test_merge_claude_settings_preserves_everything_it_does_not_own():
    existing = {
        "permissions": {"allow": ["Bash(git status)"]},
        "env": {"MY_OWN_VAR": "keep me", "ANTHROPIC_MODEL": "something-else"},
        "statusLine": {"type": "command"},
    }
    merged = byok.merge_claude_settings(existing, byok.render_claude_settings(make_spec()))
    assert merged["permissions"] == existing["permissions"]
    assert merged["statusLine"] == existing["statusLine"]
    assert merged["env"]["MY_OWN_VAR"] == "keep me"
    assert merged["env"]["ANTHROPIC_MODEL"] == "yangble5"


def test_merge_claude_settings_does_not_mutate_the_input():
    existing = {"env": {"A": "1"}}
    byok.merge_claude_settings(existing, byok.render_claude_settings(make_spec()))
    assert existing == {"env": {"A": "1"}}


def test_merge_claude_settings_handles_a_missing_file():
    merged = byok.merge_claude_settings(None, byok.render_claude_settings(make_spec()))
    assert merged["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"


def test_merge_claude_settings_refuses_a_non_object_env():
    with pytest.raises(byok.SetupError, match="non-object"):
        byok.merge_claude_settings({"env": "nope"}, byok.render_claude_settings(make_spec()))


def test_codex_toml_is_valid_toml_with_the_context_unlock_and_no_secret():
    spec = make_spec()
    text = byok.render_codex_toml(spec)
    parsed = tomllib.loads(text)
    assert parsed["model"] == "yangble5"
    assert parsed["model_context_window"] == 1_000_000
    assert parsed["model_providers"]["yangble5"]["base_url"] == "http://127.0.0.1:8318/v1"
    assert parsed["model_providers"]["yangble5"]["env_key"] == "YANGBLE5_API_KEY"
    assert spec.local_key not in text


def test_codex_toml_survives_an_alias_that_is_not_a_bare_toml_key():
    """`yangble5[1m]` is a legal alias and an illegal bare TOML key; the provider
    table stays keyed on a fixed name so the alias only ever appears quoted."""
    parsed = tomllib.loads(byok.render_codex_toml(make_spec(alias="yangble5[1m]")))
    assert parsed["model"] == "yangble5[1m]"
    assert "yangble5" in parsed["model_providers"]


def test_env_files_carry_the_local_key_and_the_isolated_client_dirs():
    spec = make_spec()
    for text in (byok.render_env_sh(spec), byok.render_env_ps1(spec)):
        assert spec.local_key in text
        assert "CLAUDE_CONFIG_DIR" in text
        assert "CODEX_HOME" in text
        assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" in text


def test_env_sh_quotes_a_path_containing_a_single_quote():
    spec = make_spec(out_dir=Path("/home/o'brien/.yangble5"))
    assert "'\\''" in byok.render_env_sh(spec)


# ------------------------------------------------------- write_with_backup ---


def test_write_creates_a_new_file(tmp_path):
    target = tmp_path / "config.yaml"
    status, backup = byok.write_with_backup(target, "hello\n", now=NOW)
    assert (status, backup) == ("created", None)
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_writing_identical_content_is_a_no_op_with_no_backup(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("same\n", encoding="utf-8")
    status, backup = byok.write_with_backup(target, "same\n", now=NOW)
    assert (status, backup) == ("unchanged", None)
    assert list(tmp_path.iterdir()) == [target]


def test_an_existing_file_is_backed_up_and_never_clobbered(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("ORIGINAL", encoding="utf-8")
    status, backup = byok.write_with_backup(target, "NEW", now=NOW)
    assert status == "replaced"
    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "ORIGINAL"
    assert target.read_text(encoding="utf-8") == "NEW"


def test_two_writes_in_the_same_second_do_not_overwrite_the_first_backup(tmp_path):
    """A backup that a second run can destroy is not a backup."""
    target = tmp_path / "settings.json"
    target.write_text("V1", encoding="utf-8")
    _, first = byok.write_with_backup(target, "V2", now=NOW)
    _, second = byok.write_with_backup(target, "V3", now=NOW)
    assert first != second
    assert first.read_text(encoding="utf-8") == "V1"
    assert second.read_text(encoding="utf-8") == "V2"
    assert target.read_text(encoding="utf-8") == "V3"


def test_a_file_that_cannot_be_decoded_is_backed_up_byte_for_byte(tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes(b"\xff\xfe not utf-8")
    status, backup = byok.write_with_backup(target, "NEW", now=NOW)
    assert status == "replaced"
    assert backup.read_bytes() == b"\xff\xfe not utf-8"


def test_dry_run_writes_nothing_at_all(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("ORIGINAL", encoding="utf-8")
    status, _ = byok.write_with_backup(target, "NEW", now=NOW, dry_run=True)
    assert status == "replaced"
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(tmp_path.iterdir()) == [target]


def test_backup_path_for_never_returns_a_path_that_exists(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    first = byok.backup_path_for(target, NOW)
    first.write_text("x", encoding="utf-8")
    assert byok.backup_path_for(target, NOW) != first


def test_read_json_if_present_returns_none_for_a_missing_file(tmp_path):
    assert byok.read_json_if_present(tmp_path / "nope.json") is None


def test_read_json_if_present_refuses_to_silently_replace_a_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(byok.SetupError, match="not readable JSON"):
        byok.read_json_if_present(path)


def test_read_json_if_present_refuses_a_json_array(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(byok.SetupError, match="not an object"):
        byok.read_json_if_present(path)


# --------------------------------------------------------- oauth detection ---


def test_detect_oauth_credentials_on_a_missing_or_empty_dir(tmp_path):
    assert byok.detect_oauth_credentials(tmp_path / "nope") == []
    assert byok.detect_oauth_credentials(tmp_path) == []


def test_detect_oauth_credentials_finds_token_files(tmp_path):
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert [p.name for p in byok.detect_oauth_credentials(tmp_path)] == ["a.json", "b.json"]


# ------------------------------------------------------- printed shim command ---


def test_the_printed_shim_command_uses_flags_the_shim_actually_accepts():
    """Guards against printed instructions drifting away from the real tool.

    The shim takes --listen-host/--listen-port, not a combined --listen; an
    earlier draft of setup.py printed the latter and it would simply have
    failed in the user's terminal.
    """
    from tools import claude_shim

    parser = claude_shim.build_parser({})
    accepted = {opt for action in parser._actions for opt in action.option_strings}
    flags = [token for token in byok.shim_command(make_spec()) if token.startswith("--")]
    assert flags
    assert set(flags) <= accepted

    # And it must actually parse, not merely use known flag names.
    parsed = parser.parse_args(byok.shim_command(make_spec())[2:])
    assert parsed.listen_port == 8320
    assert parsed.upstream == "http://127.0.0.1:8318"


def test_no_shim_prints_no_shim_command():
    assert byok.shim_command(make_spec(shim_port=None)) == []


# ------------------------------------------------------ bench interpretation ---


def bench_payload(rate: float, warm: int = 2, notes=()) -> dict:
    prompt = 100_000 * warm
    return {
        "eligible_hit_rate": rate,
        "cached_tokens": int(prompt * rate),
        "prompt_tokens": prompt,
        "cold_round": {"prompt_total": 100_000, "cache_read": 0, "latency_ms": 21_000},
        "warm_rounds": [{"round": n} for n in range(2, 2 + warm)],
        "notes": list(notes),
    }


def test_a_good_measurement_passes_and_still_reports_the_caveat():
    ok, lines = byok.interpret_bench(bench_payload(0.9953), 0.80)
    assert ok
    body = "\n".join(lines)
    assert "99.53%" in body
    assert "WARM-round" in body
    assert "cold round 1" in body


def test_a_low_measurement_fails_and_prints_the_pool_diagnostic_first():
    ok, lines = byok.interpret_bench(bench_payload(0.49), 0.80)
    assert not ok
    body = "\n".join(lines)
    assert "49.00%" in body
    assert "BELOW TARGET" in body
    assert "nextModelPoolOffset" in body
    assert "listed exactly ONCE" in body


def test_no_warm_rounds_cannot_pass_however_good_the_number_looks():
    payload = bench_payload(1.0, warm=0)
    ok, _ = byok.interpret_bench(payload, 0.80)
    assert not ok


def test_zero_prompt_tokens_cannot_pass():
    payload = bench_payload(1.0)
    payload["prompt_tokens"] = 0
    ok, _ = byok.interpret_bench(payload, 0.80)
    assert not ok


def test_notes_from_the_benchmark_are_surfaced_verbatim():
    ok, lines = byok.interpret_bench(bench_payload(0.0, notes=["upstream returned ZERO"]), 0.80)
    assert not ok
    assert any("upstream returned ZERO" in line for line in lines)


def test_an_empty_payload_is_treated_as_a_failure_not_a_crash():
    ok, lines = byok.interpret_bench({}, 0.80)
    assert not ok
    assert lines


# ------------------------------------------------------------- the CLI flow ---


def test_parser_defaults_match_the_documented_ones():
    args = byok.build_parser().parse_args([])
    assert args.engine_port == 8318
    assert args.shim_port == 8320
    assert args.alias == "yangble5"
    assert args.session_ttl == "12h"
    assert args.max_context == 1_000_000


def test_the_parser_has_no_flag_that_accepts_a_credential():
    """argv is readable by other processes and lands in shell history."""
    actions = {opt for action in byok.build_parser()._actions for opt in action.option_strings}
    assert "--api-key" not in actions
    assert "--upstream-key" not in actions
    assert "--upstream-key-env" in actions


def test_dry_run_end_to_end_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("YANGBLE5_UPSTREAM_KEY", "AIzaSyFAKEfakeFAKEfake0123456789abcdef")
    out = tmp_path / "byok"
    code = byok.main(
        [
            "--provider",
            "gemini-api-key",
            "--model",
            "gemini-2.5-pro",
            "--out-dir",
            str(out),
            "--non-interactive",
            "--dry-run",
            "--quiet",
        ]
    )
    assert code == 0
    assert not out.exists() or not any(out.rglob("*.yaml"))


def test_end_to_end_writes_the_expected_files(tmp_path, monkeypatch):
    monkeypatch.setenv("YANGBLE5_UPSTREAM_KEY", "AIzaSyFAKEfakeFAKEfake0123456789abcdef")
    out = tmp_path / "byok"
    code = byok.main(
        [
            "--provider",
            "gemini-api-key",
            "--model",
            "gemini-2.5-pro",
            "--out-dir",
            str(out),
            "--non-interactive",
            "--skip-bench",
            "--quiet",
        ]
    )
    assert code == 0
    config = yaml.safe_load((out / "config.yaml").read_text(encoding="utf-8"))
    assert config["routing"]["strategy"] == "fill-first"
    settings = json.loads((out / "claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"
    codex = tomllib.loads((out / "codex" / "config.toml").read_text(encoding="utf-8"))
    assert codex["model_context_window"] == 1_000_000
    assert (out / "env.sh").exists()
    assert (out / "env.ps1").exists()


def test_a_second_run_backs_up_the_first_run_rather_than_clobbering_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("YANGBLE5_UPSTREAM_KEY", "AIzaSyFAKEfakeFAKEfake0123456789abcdef")
    out = tmp_path / "byok"
    argv = [
        "--provider",
        "gemini-api-key",
        "--model",
        "gemini-2.5-pro",
        "--out-dir",
        str(out),
        "--non-interactive",
        "--skip-bench",
        "--quiet",
    ]
    assert byok.main(argv) == 0
    first = (out / "config.yaml").read_text(encoding="utf-8")
    assert byok.main(argv) == 0  # a fresh local key makes the content differ
    backups = list(out.glob("config.yaml" + byok.BACKUP_SUFFIX + "*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == first


def test_a_run_with_no_credential_writes_nothing_and_exits_two(tmp_path, monkeypatch):
    monkeypatch.delenv("YANGBLE5_UPSTREAM_KEY", raising=False)
    out = tmp_path / "byok"
    code = byok.main(
        [
            "--provider",
            "gemini-api-key",
            "--model",
            "gemini-2.5-pro",
            "--out-dir",
            str(out),
            "--non-interactive",
            "--skip-bench",
            "--quiet",
        ]
    )
    assert code == 2
    assert not (out / "config.yaml").exists()
