"""Tests for the system-role streaming workaround in tools/claude_shim.py.

The contract under test is narrow on purpose:

1. A ``messages[]`` entry with ``role: "system"`` becomes ``role: "user"``.
2. Anything the shim does not need to fix comes back as the SAME bytes -- not
   equal-after-reparsing, byte-identical. The upstream prompt cache keys on the
   exact request bytes, so a gratuitous re-serialisation would cost cache hits on
   every conversation that never had the bug.
3. Nothing outside POST /v1/messages is touched.
"""

from __future__ import annotations

import json

import pytest

from tools.claude_shim import (
    ShimConfig,
    fix_system_roles,
    maybe_fix_body,
    parse_upstream,
    should_rewrite,
)


def encode(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------- mapping ---


def test_mid_conversation_system_role_becomes_user():
    """The exact shape Claude Code 2.1.x injects: system message in the middle."""
    body = encode(
        {
            "model": "yangble5",
            "system": "you are a coding agent",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "system", "content": "Available agents: general-purpose"},
                {"role": "user", "content": "go"},
            ],
        }
    )
    result = json.loads(fix_system_roles(body))
    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "user", "user"]
    # The top-level Anthropic `system` parameter is not a message and must survive.
    assert result["system"] == "you are a coding agent"
    # Content is carried through untouched.
    assert result["messages"][2]["content"] == "Available agents: general-purpose"


def test_every_system_message_is_mapped_not_just_the_first():
    body = encode(
        {
            "messages": [
                {"role": "system", "content": "a"},
                {"role": "user", "content": "b"},
                {"role": "system", "content": "c"},
            ]
        }
    )
    roles = [m["role"] for m in json.loads(fix_system_roles(body))["messages"]]
    assert roles == ["user", "user", "user"]


def test_mapping_is_idempotent():
    body = encode({"messages": [{"role": "system", "content": "x"}]})
    once = fix_system_roles(body)
    twice = fix_system_roles(once)
    assert json.loads(twice)["messages"][0]["role"] == "user"
    # Second pass finds no system role, so it must return the same object bytes.
    assert twice is once


def test_non_ascii_content_is_not_escaped_away():
    """ensure_ascii=False keeps the rewritten body the same size class as the input."""
    body = encode({"messages": [{"role": "system", "content": "繁體中文 agent list"}]})
    fixed = fix_system_roles(body)
    assert "繁體中文".encode() in fixed
    assert json.loads(fixed)["messages"][0]["content"] == "繁體中文 agent list"


# ------------------------------------------------- byte-identical passthrough ---


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("no system token at all", encode({"messages": [{"role": "user", "content": "hi"}]})),
        (
            "top-level system parameter only",
            encode({"system": "prompt", "messages": [{"role": "user", "content": "hi"}]}),
        ),
        (
            "the word system inside content",
            encode({"messages": [{"role": "user", "content": 'the "system" is down'}]}),
        ),
        (
            "role system nested in a tool result, not a message role",
            encode(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "tool_result", "content": '{"role":"system"}'}],
                        }
                    ]
                }
            ),
        ),
        ("messages missing entirely", encode({"system": "prompt", "model": "yangble5"})),
        ("messages is not a list", encode({"messages": {"role": "system"}})),
        ("top-level json is not an object", encode(["system"])),
        ("empty body", b""),
    ],
)
def test_untouched_bodies_are_returned_byte_identical(label, body):
    result = fix_system_roles(body)
    assert result == body, label
    # Identity, not just equality: proves no re-serialisation round trip happened.
    assert result is body, label


@pytest.mark.parametrize(
    "body",
    [
        b'{"messages": [{"role": "system"',  # truncated
        b'{"system": not json}',
        b'\xff\xfe garbage "system" bytes',  # UTF-16 BOM, undecodable payload
        b'"system"',  # valid JSON, but a bare string
    ],
)
def test_malformed_bodies_pass_through_untouched(body):
    """A body we cannot parse is the upstream's problem to report, not ours to guess."""
    assert fix_system_roles(body) is body


# --------------------------------------------------------------- routing ---


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/v1/messages", True),
        ("POST", "/v1/messages?beta=true", True),
        ("post", "/v1/messages", True),
        ("GET", "/v1/messages", False),
        ("POST", "/v1/messages/count_tokens", False),
        ("POST", "/v1/chat/completions", False),
        ("POST", "/v0/management/usage-queue", False),
        ("POST", "/v1/models", False),
        ("POST", "/", False),
    ],
)
def test_should_rewrite_only_matches_the_messages_endpoint(method, path, expected):
    assert should_rewrite(method, path) is expected


def test_maybe_fix_body_leaves_other_endpoints_alone():
    """A /v1/chat/completions body may legitimately carry a system role."""
    body = encode({"messages": [{"role": "system", "content": "you are helpful"}]})
    assert maybe_fix_body("POST", "/v1/chat/completions", body) is body
    assert maybe_fix_body("GET", "/v1/messages", body) is body
    assert maybe_fix_body("POST", "/v1/messages", None) is None
    assert maybe_fix_body("POST", "/v1/messages", b"") == b""


def test_maybe_fix_body_rewrites_on_the_messages_endpoint():
    body = encode({"messages": [{"role": "system", "content": "agents"}]})
    fixed = maybe_fix_body("POST", "/v1/messages?beta=x", body)
    assert fixed is not None
    assert json.loads(fixed)["messages"][0]["role"] == "user"


# --------------------------------------------------------------- config ---


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8318", ("http", "127.0.0.1", 8318)),
        ("http://127.0.0.1:8318/", ("http", "127.0.0.1", 8318)),
        ("127.0.0.1:8318", ("http", "127.0.0.1", 8318)),
        ("  localhost:9000  ", ("http", "localhost", 9000)),
        ("http://engine.internal", ("http", "engine.internal", 80)),
        ("https://engine.internal", ("https", "engine.internal", 443)),
        ("https://engine.internal:8443/v1", ("https", "engine.internal", 8443)),
    ],
)
def test_parse_upstream(url, expected):
    assert parse_upstream(url) == expected


@pytest.mark.parametrize("url", ["ftp://host:21", "://nohost", "http://"])
def test_parse_upstream_rejects_unusable_urls(url):
    with pytest.raises(ValueError):
        parse_upstream(url)


def test_shim_config_from_parts_picks_the_right_connection_class():
    plain = ShimConfig.from_parts("127.0.0.1", 8320, "http://127.0.0.1:8318")
    assert (plain.upstream_host, plain.upstream_port, plain.upstream_scheme) == (
        "127.0.0.1",
        8318,
        "http",
    )
    secure = ShimConfig.from_parts("0.0.0.0", 9999, "https://proxy.example.com")
    assert secure.upstream_port == 443
    assert "https://proxy.example.com:443" in secure.describe()
