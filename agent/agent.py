"""Chat assistant for DMs and @mentions, backed by Claude.

The assistant runs in one of two modes:

* **Claude mode** - an Anthropic credential (``ANTHROPIC_API_KEY`` or
  ``ANTHROPIC_AUTH_TOKEN``) is set: Claude answers, using the model and effort
  from the instance's ``[assistant]`` settings.
* **Basic mode** - no credential: the bot replies with a fixed message pointing
  to the slash commands. Slash commands never depend on the assistant.
"""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

import anthropic

from agent.deps import AgentDeps
from agent.tools import TOOLS, run_tool
from instance import InstanceConfig, get_instance

logger = logging.getLogger(__name__)

CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
MAX_MODEL_CALLS = 6  # per user message: the reply plus a few tool round trips

SYSTEM_PROMPT_TEMPLATE = """\
You are {name}, a careful assistant for {label} sequencing data.

Your role is to explain registry status, help users identify missing metadata, and guide
them to the deterministic Slack commands `{status}`, `{sequencing_accession}`,
and `{reference_accession}`.

Never claim that you inspected, registered, moved, deleted, or modified a file unless a
tool result in the current conversation proves it. Never invent sample metadata. Registry
writes require the structured accession form and explicit human approval.

## PERSONALITY
- Friendly, helpful, and approachable
- Professional and approachable
- Concise and clear — respect people's time
- Confident but honest when you don't know something

## RESPONSE GUIDELINES
- Keep responses to 3 sentences max — be punchy, scannable, and actionable
- End with a clear next step on its own line so it's easy to spot
- Use a bullet list only for multi-step instructions
- Use casual, conversational language
- Use emoji sparingly — at most one per message, and only to set tone

## FORMATTING RULES
- Use standard Markdown syntax: **bold**, _italic_, `code`, ```code blocks```, > blockquotes
- Use bullet points for multi-step instructions

## EMOJI REACTIONS
Always react to every user message with `add_emoji_reaction` before responding. \
Pick any Slack emoji that reflects the *topic* or *tone* of the message — be creative and specific \
(e.g. `dog` for dog topics, `books` for learning, `wave` for greetings). \
Vary your picks across a thread; don't repeat the same emoji.
"""


@dataclass
class AgentReply:
    """What to post, and the conversation to store (None = leave stored history as is)."""

    text: str
    messages: list | None


def assistant_enabled() -> bool:
    """True when an Anthropic credential is configured (placeholders don't count)."""
    for name in CREDENTIAL_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value and not value.startswith("YOUR_"):
            return True
    return False


def basic_reply_text(instance: InstanceConfig) -> str:
    """Fixed reply used when the Claude assistant is not available."""
    return (
        f"Hi! I'm {instance.name}. My chat assistant isn't turned on yet, "
        "but these commands work right now:\n"
        f"• `{instance.command('status')}` — show new and missing files\n"
        f"• `{instance.command('sequencing-accession')}` — review and register sequencing files\n"
        f"• `{instance.command('reference-accession')}` — review and register reference files\n"
        f"• `{instance.command('help')}` — show command help"
    )


def build_system_prompt(instance: InstanceConfig) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        name=instance.name,
        label=instance.label,
        status=instance.command("status"),
        sequencing_accession=instance.command("sequencing-accession"),
        reference_accession=instance.command("reference-accession"),
    )


@lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _echoable_content(content: list) -> list:
    """Assistant content to append to history.

    After a server-side fallback, model-internal blocks (thinking, tool calls) that came
    *before* the last ``fallback`` marker must not be sent back; text and everything after
    the marker are kept. Without a fallback the content is kept unchanged (append-only).
    """
    boundary = max((i for i, block in enumerate(content) if block.type == "fallback"), default=None)
    if boundary is None:
        return list(content)
    return [b for i, b in enumerate(content) if i >= boundary or b.type == "text"]


def run_agent(history: list | None, text: str, deps: AgentDeps) -> AgentReply:
    """Answer one user message. ``history`` is the stored conversation for this thread."""
    instance = get_instance()
    if not assistant_enabled():
        return AgentReply(text=basic_reply_text(instance), messages=None)

    messages = list(history or []) + [{"role": "user", "content": text}]
    params = {
        "model": instance.assistant_model,
        "max_tokens": MAX_TOKENS,
        "system": build_system_prompt(instance),
        "tools": TOOLS,
        "output_config": {"effort": instance.assistant_effort},
    }
    if instance.assistant_fallbacks:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"

    try:
        for _ in range(MAX_MODEL_CALLS):
            response = _client().beta.messages.create(messages=messages, **params)

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                logger.warning("Assistant request declined (stop_details=%s)", details)
                return AgentReply(
                    text="Sorry, I can't help with that one. Try rephrasing, or use "
                    f"`{instance.command('help')}` to see what I can do.",
                    messages=None,  # drop this turn; the stored history stays as it was
                )

            content = _echoable_content(response.content)
            messages.append({"role": "assistant", "content": content})

            if response.stop_reason == "tool_use":
                results = []
                for block in content:
                    if block.type == "tool_use":
                        result, is_error = run_tool(block.name, block.input, deps)
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                            "is_error": is_error,
                        })
                messages.append({"role": "user", "content": results})
                continue

            reply = "\n\n".join(b.text for b in content if b.type == "text" and b.text.strip()).strip()
            if response.stop_reason == "max_tokens":
                reply += "\n\n_(My reply was cut short.)_"
            return AgentReply(text=reply or "I don't have anything to add.", messages=messages)

        logger.warning("Assistant stopped after %d model calls without a final reply", MAX_MODEL_CALLS)
        return AgentReply(text="Sorry, I got stuck on that one. Please try again.", messages=None)

    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        logger.exception("Anthropic credential was rejected; answering in basic mode")
        return AgentReply(text=basic_reply_text(instance), messages=None)
