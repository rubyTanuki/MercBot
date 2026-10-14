import datetime

from discord import Poll
from discord.http import HTTPClient, Route, handle_message_parameters
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from MercBot.models import Interaction, PhotoIssue

PHOTO_POLL_COMMAND = "photo-poll"
COMMANDS = [
    {
        "name": PHOTO_POLL_COMMAND,
        "description": "Post a poll for each unassigned photo task in Linear",
        "type": 1,  # CHAT_INPUT
    },
]

POLL_DURATION = datetime.timedelta(days=1)
POLL_ANSWER = "I want this"
POLL_ANSWER_EMOJI = "✋"
POLL_QUESTION_MAX = 300

EPHEMERAL = 1 << 6

# Interaction response types
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5


def pong() -> dict:
    return {"type": PONG}


def deferred_ephemeral() -> dict:
    return {"type": DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, "data": {"flags": EPHEMERAL}}


def ephemeral_message(content: str) -> dict:
    return {"type": CHANNEL_MESSAGE_WITH_SOURCE, "data": {"content": content, "flags": EPHEMERAL}}


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
    """REST-only Discord client: registers commands and posts photo polls with the bot token."""

    def __init__(self, http: HTTPClient, application_id: int, guild_id: int):
        self._http = http
        self._application_id = application_id
        self._guild_id = guild_id

    async def register_commands(self) -> None:
        await self._http.bulk_upsert_guild_commands(self._application_id, self._guild_id, COMMANDS)

    async def post_photo_polls(self, interaction: Interaction, issues: list[PhotoIssue]) -> None:
        if not issues:
            await self.edit_status(interaction, "No unassigned photo tasks found.")
            return
        for issue in issues:
            await self._send_poll(interaction.channel_id, issue)
        await self.delete_status(interaction)

    async def edit_status(self, interaction: Interaction, content: str) -> None:
        """Replace the ephemeral 'thinking…' message with `content`."""
        await self._http.request(self._original_response_route("PATCH", interaction), json={"content": content})

    async def delete_status(self, interaction: Interaction) -> None:
        await self._http.request(self._original_response_route("DELETE", interaction))

    async def _send_poll(self, channel_id: int, issue: PhotoIssue) -> None:
        question = issue.title
        if issue.due_date:
            question += f" (due {issue.due_date:%a %b} {issue.due_date.day})"
        poll = Poll(question=question[:POLL_QUESTION_MAX], duration=POLL_DURATION)
        poll.add_answer(text=POLL_ANSWER, emoji=POLL_ANSWER_EMOJI)

        # Masked link with <> so Discord doesn't add a Linear preview embed above the poll.
        with handle_message_parameters(content=f"[Open in Linear](<{issue.url}>)", poll=poll) as params:
            await self._http.send_message(channel_id, params=params)

    def _original_response_route(self, method: str, interaction: Interaction) -> Route:
        return Route(
            method,
            "/webhooks/{webhook_id}/{webhook_token}/messages/@original",
            webhook_id=interaction.application_id,
            webhook_token=interaction.token,
        )
