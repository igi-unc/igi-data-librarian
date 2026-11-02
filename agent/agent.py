import asyncio
import logging
from functools import lru_cache

from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

from agent.deps import AgentDeps
from agent.tools import add_emoji_reaction
from instance import InstanceConfig, get_instance

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

logger = logging.getLogger(__name__)

SLACK_MCP_URL = "https://mcp.slack.com/mcp"

def build_system_prompt(instance: InstanceConfig) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        name=instance.name,
        label=instance.label,
        status=instance.command("status"),
        sequencing_accession=instance.command("sequencing-accession"),
        reference_accession=instance.command("reference-accession"),
    )


@lru_cache(maxsize=None)
def _build_agent(instance: InstanceConfig) -> Agent[AgentDeps]:
    return Agent[AgentDeps](
        name=instance.name,
        instructions=build_system_prompt(instance),
        tools=[add_emoji_reaction],
        model="gpt-4.1-mini",
    )


def get_agent() -> Agent[AgentDeps]:
    """Return the assistant for the active instance."""
    return _build_agent(get_instance())


def run_agent(input_items, deps):
    """Run the agent, optionally connecting to the Slack MCP server."""
    if deps.user_token:
        logger.info("Slack MCP Server enabled (user_token present)")
        return asyncio.run(_run_with_mcp(input_items, deps))

    logger.info("Slack MCP Server disabled (no user_token)")
    return Runner.run_sync(get_agent(), input=input_items, context=deps)


async def _run_with_mcp(input_items, deps):
    """Run the agent with the Slack MCP Server connected."""
    mcp_server = MCPServerStreamableHttp(
        params={
            "url": SLACK_MCP_URL,
            "headers": {"Authorization": f"Bearer {deps.user_token}"},
        },
    )
    async with mcp_server:
        agent_with_mcp = get_agent().clone(mcp_servers=[mcp_server])
        return await Runner.run(agent_with_mcp, input=input_items, context=deps)
