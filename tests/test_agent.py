import logging
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import pytest

from agent import agent as agent_module
from agent import assistant_enabled, run_agent
from agent.deps import AgentDeps
from instance import parse_instance
from listeners.events.app_mentioned import handle_app_mentioned


def _block(type_, **fields):
    return SimpleNamespace(type=type_, **fields)


def _response(stop_reason, *content, stop_details=None):
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), stop_details=stop_details)


def _deps():
    return AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.0", message_ts="1.0")


@pytest.fixture
def fake_client(monkeypatch):
    """Enable Claude mode with a fake Anthropic client whose replies each test sets."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = Mock()
    monkeypatch.setattr(agent_module, "_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for name in agent_module.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_basic_mode_without_credentials():
    assert not assistant_enabled()
    reply = run_agent(None, "hello", _deps())
    assert "/test-status" in reply.text
    assert reply.messages is None


def test_placeholder_key_is_not_a_credential(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY")
    assert not assistant_enabled()


def test_claude_mode_runs_tool_then_replies(fake_client, monkeypatch):
    monkeypatch.setattr("agent.tools.emoji_reaction.random.random", lambda: 0.99)
    thinking = _block("thinking", thinking="", signature="sig")
    tool_use = _block("tool_use", id="tu1", name="add_emoji_reaction", input={"emoji_name": "wave"})
    fake_client.beta.messages.create.side_effect = [
        _response("tool_use", thinking, tool_use),
        _response("end_turn", _block("text", text="Hi! Use /test-status.")),
    ]
    deps = _deps()
    reply = run_agent(None, "hello", deps)

    assert reply.text == "Hi! Use /test-status."
    deps.client.reactions_add.assert_called_once_with(channel="C1", timestamp="1.0", name="wave")
    # History is append-only: user, assistant (thinking + tool_use unchanged), tool result, assistant
    roles = [m["role"] for m in reply.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert reply.messages[1]["content"] == [thinking, tool_use]
    assert reply.messages[2]["content"][0]["tool_use_id"] == "tu1"

    kwargs = fake_client.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_config"] == {"effort": "medium"}
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert "Test Librarian" in kwargs["system"]
    assert "temperature" not in kwargs


def test_history_is_extended_not_rewritten(fake_client):
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": ["prior"]}]
    fake_client.beta.messages.create.return_value = _response("end_turn", _block("text", text="ok"))
    reply = run_agent(history, "next", _deps())
    assert reply.messages[:2] == history
    assert reply.messages[2] == {"role": "user", "content": "next"}


def test_refusal_keeps_previous_history(fake_client):
    fake_client.beta.messages.create.return_value = _response("refusal", stop_details={"category": "bio"})
    reply = run_agent([{"role": "user", "content": "x"}], "bad", _deps())
    assert "can't help" in reply.text
    assert reply.messages is None


def test_fallback_drops_internal_blocks_before_boundary(fake_client):
    before_thinking = _block("thinking", thinking="", signature="a")
    before_text = _block("text", text="Partial ")
    marker = _block("fallback")
    after_text = _block("text", text="answer")
    fake_client.beta.messages.create.return_value = _response(
        "end_turn", before_thinking, before_text, marker, after_text
    )
    reply = run_agent(None, "q", _deps())
    assert reply.messages[-1]["content"] == [before_text, marker, after_text]


def test_rejected_key_falls_back_to_basic_mode(fake_client):
    request = Mock()
    fake_client.beta.messages.create.side_effect = anthropic.AuthenticationError(
        "bad key", response=Mock(status_code=401, request=request, headers={}), body=None
    )
    reply = run_agent(None, "hello", _deps())
    assert "/test-help" in reply.text
    assert reply.messages is None


def test_assistant_settings_are_configurable():
    config = parse_instance({
        "name": "X", "command_prefix": "x",
        "assistant": {"model": "claude-haiku-5-5", "effort": "low", "fallbacks": False},
    })
    assert (config.assistant_model, config.assistant_effort, config.assistant_fallbacks) == (
        "claude-haiku-5-5", "low", False,
    )
    with pytest.raises(ValueError, match="effort"):
        parse_instance({"name": "X", "command_prefix": "x", "assistant": {"effort": "extreme"}})


def test_fallbacks_can_be_disabled(fake_client, monkeypatch):
    config = parse_instance({"name": "X", "command_prefix": "x", "assistant": {"fallbacks": False}})
    monkeypatch.setattr(agent_module, "get_instance", lambda: config)
    fake_client.beta.messages.create.return_value = _response("end_turn", _block("text", text="ok"))
    run_agent(None, "q", _deps())
    kwargs = fake_client.beta.messages.create.call_args.kwargs
    assert "fallbacks" not in kwargs and "betas" not in kwargs


def test_mention_in_basic_mode_replies_without_model():
    say = Mock()
    set_status = Mock()
    handle_app_mentioned(
        client=Mock(), context=Mock(channel_id="C1", user_id="U1", user_token=None),
        event={"text": "<@UBOT> hi", "ts": "1.0"}, logger=logging.getLogger(__name__),
        say=say, say_stream=Mock(), set_status=set_status,
    )
    say.assert_called_once()
    assert "/test-status" in say.call_args.kwargs["text"]
    set_status.assert_not_called()
