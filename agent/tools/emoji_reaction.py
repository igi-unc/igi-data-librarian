import random

from slack_sdk.errors import SlackApiError

from agent.deps import AgentDeps

EMOJI_REACTION_TOOL = {
    "name": "add_emoji_reaction",
    "description": (
        "Add an emoji reaction to the user's current message to acknowledge the topic.\n\n"
        "Use any standard Slack emoji that matches the topic or tone of the message. "
        "Be creative and specific: if someone mentions a dog, use `dog`; if they sound "
        "frustrated, use `sweat_smile`. Common picks, not the full set:\n"
        "- Gratitude/praise: pray, bow, blush, sparkles, star-struck, heart\n"
        "- Frustration/confusion: thinking_face, face_with_monocle, sweat_smile, upside_down_face\n"
        "- Something broken: wrench, hammer_and_wrench, mag\n"
        "- Performance/slow: hourglass_flowing_sand, snail\n"
        "- Urgency: rotating_light, zap, fire\n"
        "- Success/celebration: tada, raised_hands, partying_face, rocket, muscle\n"
        "- Setup/config: gear, package\n"
        "- Network/connectivity: satellite, signal_strength\n"
        "- Agreement/acknowledgment: thumbsup, ok_hand, saluting_face, +1"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "emoji_name": {
                "type": "string",
                "description": "The Slack emoji name without colons (e.g. 'tada', 'wrench', 'pray').",
            },
        },
        "required": ["emoji_name"],
        "additionalProperties": False,
    },
    "strict": True,
}


def add_emoji_reaction(deps: AgentDeps, emoji_name: str) -> str:
    """React to the user's current message; returns a short result for the model."""
    # Skip ~15% of reactions to feel more natural
    if random.random() < 0.15:
        return f"Skipped :{emoji_name}: reaction (randomly omitted to avoid over-reacting)"

    try:
        deps.client.reactions_add(
            channel=deps.channel_id,
            timestamp=deps.message_ts,
            name=emoji_name,
        )
        return f"Reacted with :{emoji_name}:"
    except SlackApiError as e:
        return f"Could not add reaction: {e.response['error']}"
