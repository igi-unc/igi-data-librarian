from agent.deps import AgentDeps

from .emoji_reaction import EMOJI_REACTION_TOOL, add_emoji_reaction

# Tool definitions sent to Claude, in a fixed order (keeps the prompt cache stable).
TOOLS = [EMOJI_REACTION_TOOL]


def run_tool(name: str, tool_input: dict, deps: AgentDeps) -> tuple[str, bool]:
    """Execute a tool call from Claude. Returns (result text, is_error)."""
    if name == EMOJI_REACTION_TOOL["name"]:
        emoji_name = tool_input.get("emoji_name") if isinstance(tool_input, dict) else None
        if not isinstance(emoji_name, str) or not emoji_name.strip():
            return "Error: emoji_name must be a non-empty string.", True
        return add_emoji_reaction(deps, emoji_name.strip().strip(":")), False
    return f"Error: unknown tool {name!r}.", True


__all__ = ["TOOLS", "run_tool", "add_emoji_reaction"]
