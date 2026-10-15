import datetime

from discord import Poll
from discord.http import HTTPClient, Route, handle_message_parameters
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from MercBot.models import Interaction, Issue

POLL_COMMAND = "poll"
TAG_OPTION = "tag"
TAG_MAX_LENGTH = 100
COMMANDS = [
    {
        "name": POLL_COMMAND,
        "description": "Post a poll of unassigned Linear issues with a given tag",
        "type": 1,  # CHAT_INPUT
        "options": [
            {
                "name": TAG_OPTION,
                "description": "Linear label to filter by, e.g. Photography",
                "type": 3,  # STRING
                "required": True,
                "min_length": 1,
                "max_length": TAG_MAX_LENGTH,
                "autocomplete": True,
            },
        ],
    },
]

POLL_QUESTION = "Which {tag} tasks do you want to take?"
POLL_DURATION = datetime.timedelta(days=1)
POLL_MAX_ANSWERS = 10
POLL_ANSWER_MAX = 55
NO_ARTICLE_ANSWER = "No article"
# One slot per poll is reserved for NO_ARTICLE_ANSWER.
ISSUES_PER_POLL = POLL_MAX_ANSWERS - 1

EPHEMERAL = 1 << 6

# Interaction response types
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5
APPLICATION_COMMAND_AUTOCOMPLETE_RESULT = 8

MAX_AUTOCOMPLETE_CHOICES = 25


def pong() -> dict:
    return {"type": PONG}


def deferred_ephemeral() -> dict:
    return {"type": DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, "data": {"flags": EPHEMERAL}}


def ephemeral_message(content: str) -> dict:
    return {"type": CHANNEL_MESSAGE_WITH_SOURCE, "data": {"content": content, "flags": EPHEMERAL}}


def tag_suggestions(labels: list[str], typed: str) -> dict:
    """Autocomplete response: labels starting with `typed` first, then ones containing it."""
    typed = typed.strip().casefold()
    starts = [label for label in labels if label.casefold().startswith(typed)]
    contains = [label for label in labels if typed in label.casefold() and label not in starts]
    choices = [
        {"name": label, "value": label}
        for label in starts + contains
        if len(label) <= TAG_MAX_LENGTH
    ][:MAX_AUTOCOMPLETE_CHOICES]
    return {"type": APPLICATION_COMMAND_AUTOCOMPLETE_RESULT, "data": {"choices": choices}}


class InteractionVerifier:
    """Checks the ed25519 signature Discord attaches to every interaction request."""

    def __init__(self, public_key: str):
        self._key = VerifyKey(bytes.fromhex(public_key))

    def verify(self, signature: str, timestamp: str, body: bytes) -> bool:
        try:
            self._key.verify(timestamp.encode() + body, bytes.fromhex(signature))
        except (BadSignatureError, ValueError):
            return False
        return True


class DiscordBot:
    """REST-only Discord client: registers commands and posts task polls with the bot token."""

    def __init__(self, http: HTTPClient, application_id: int, guild_id: int):
        self._http = http
        self._application_id = application_id
        self._guild_id = guild_id

    async def register_commands(self) -> None:
        await self._http.bulk_upsert_guild_commands(self._application_id, self._guild_id, COMMANDS)

    async def post_polls(self, interaction: Interaction, tag: str, issues: list[Issue]) -> None:
        if not issues:
            await self.edit_status(interaction, f"No unassigned tasks tagged \"{tag}\" found.")
            return
        # Discord caps polls at 10 answers, so larger batches are split across several polls.
        batches = [issues[i : i + ISSUES_PER_POLL] for i in range(0, len(issues), ISSUES_PER_POLL)]
        for n, batch in enumerate(batches, start=1):
            question = POLL_QUESTION.format(tag=tag)
            if len(batches) > 1:
                question += f" ({n}/{len(batches)})"
            await self._send_poll(interaction.channel_id, question, batch)
        await self.delete_status(interaction)

    async def edit_status(self, interaction: Interaction, content: str) -> None:
        """Replace the ephemeral 'thinking…' message with `content`."""
        await self._http.request(self._original_response_route("PATCH", interaction), json={"content": content})

    async def delete_status(self, interaction: Interaction) -> None:
        await self._http.request(self._original_response_route("DELETE", interaction))

    async def _send_poll(self, channel_id: int, question: str, issues: list[Issue]) -> None:
        poll = Poll(question=question, duration=POLL_DURATION, multiple=True)
        for issue in issues:
            text = issue.title if len(issue.title) <= POLL_ANSWER_MAX else issue.title[: POLL_ANSWER_MAX - 1] + "…"
            poll.add_answer(text=text)
        poll.add_answer(text=NO_ARTICLE_ANSWER)

        with handle_message_parameters(poll=poll) as params:
            await self._http.send_message(channel_id, params=params)

    def _original_response_route(self, method: str, interaction: Interaction) -> Route:
        return Route(
            method,
            "/webhooks/{webhook_id}/{webhook_token}/messages/@original",
            webhook_id=interaction.application_id,
            webhook_token=interaction.token,
        )
