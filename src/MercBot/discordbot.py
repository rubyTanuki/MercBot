import datetime

from discord import Poll
from discord.http import HTTPClient, Route, handle_message_parameters
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from MercBot.models import Interaction, Issue, PollDraft

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
DEFERRED_UPDATE_MESSAGE = 6
UPDATE_MESSAGE = 7
APPLICATION_COMMAND_AUTOCOMPLETE_RESULT = 8

MAX_AUTOCOMPLETE_CHOICES = 25

# Review message components. custom_id is "<REVIEW_PREFIX>:<action>:<draft id>[:<select index>]".
REVIEW_PREFIX = "review"
REVIEW_SELECT = "select"
REVIEW_POST = "post"
REVIEW_CANCEL = "cancel"
SELECT_MAX_OPTIONS = 25
SELECT_LABEL_MAX = 100
# Discord allows 5 component rows; one holds the buttons.
MAX_REVIEW_ISSUES = 4 * SELECT_MAX_OPTIONS

ACTION_ROW, BUTTON, STRING_SELECT = 1, 2, 3
BUTTON_SECONDARY, BUTTON_SUCCESS = 2, 3


def pong() -> dict:
    return {"type": PONG}


def deferred_ephemeral() -> dict:
    return {"type": DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, "data": {"flags": EPHEMERAL}}


def ephemeral_message(content: str) -> dict:
    return {"type": CHANNEL_MESSAGE_WITH_SOURCE, "data": {"content": content, "flags": EPHEMERAL}}


def deferred_update() -> dict:
    """Acknowledge a component click without changing its message."""
    return {"type": DEFERRED_UPDATE_MESSAGE}


def update_message(content: str) -> dict:
    """Replace a component's message with plain text, removing its components."""
    return {"type": UPDATE_MESSAGE, "data": {"content": content, "components": []}}


def review_message(draft_id: str, draft: PollDraft, truncated: bool) -> dict:
    """Ephemeral review step: every issue starts ticked; unticked ones are left out of the poll."""
    count = len(draft.titles)
    content = (
        f"**{count} unassigned {draft.tag} task{'s' if count != 1 else ''}"
        f"{f' (showing the first {MAX_REVIEW_ISSUES})' if truncated else ''}.** "
        "Untick any you don't want in the poll, then press **Post poll**."
    )
    rows = []
    for n, start in enumerate(range(0, count, SELECT_MAX_OPTIONS)):
        chunk = range(start, min(start + SELECT_MAX_OPTIONS, count))
        placeholder = "Tasks" if count <= SELECT_MAX_OPTIONS else f"Tasks {chunk.start + 1}-{chunk.stop}"
        options = [
            {"label": _truncate(draft.titles[i], SELECT_LABEL_MAX), "value": str(i), "default": i in draft.selected}
            for i in chunk
        ]
        select = {
            "type": STRING_SELECT,
            "custom_id": f"{REVIEW_PREFIX}:{REVIEW_SELECT}:{draft_id}:{n}",
            "placeholder": placeholder,
            "min_values": 0,
            "max_values": len(options),
            "options": options,
        }
        rows.append({"type": ACTION_ROW, "components": [select]})
    buttons = [
        {"type": BUTTON, "style": BUTTON_SUCCESS, "label": "Post poll", "custom_id": f"{REVIEW_PREFIX}:{REVIEW_POST}:{draft_id}"},
        {"type": BUTTON, "style": BUTTON_SECONDARY, "label": "Cancel", "custom_id": f"{REVIEW_PREFIX}:{REVIEW_CANCEL}:{draft_id}"},
    ]
    rows.append({"type": ACTION_ROW, "components": buttons})
    return {"content": content, "components": rows}


def parse_review_id(custom_id: str) -> tuple[str, str, int | None] | None:
    """(action, draft id, select index) for a review component's custom_id, or None if it isn't one."""
    parts = custom_id.split(":")
    if len(parts) == 3 and parts[0] == REVIEW_PREFIX and parts[1] in (REVIEW_POST, REVIEW_CANCEL):
        return parts[1], parts[2], None
    if len(parts) == 4 and parts[0] == REVIEW_PREFIX and parts[1] == REVIEW_SELECT and parts[3].isdigit():
        return parts[1], parts[2], int(parts[3])
    return None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


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

    async def send_polls(self, channel_id: int, tag: str, issues: list[Issue]) -> None:
        # Discord caps polls at 10 answers, so larger batches are split across several polls.
        batches = [issues[i : i + ISSUES_PER_POLL] for i in range(0, len(issues), ISSUES_PER_POLL)]
        for n, batch in enumerate(batches, start=1):
            question = POLL_QUESTION.format(tag=tag)
            if len(batches) > 1:
                question += f" ({n}/{len(batches)})"
            await self._send_poll(channel_id, question, batch)

    async def edit_status(self, interaction: Interaction, content: str) -> None:
        """Replace the interaction's ephemeral message with plain `content`."""
        await self.edit_original(interaction, {"content": content, "components": []})

    async def edit_original(self, interaction: Interaction, message: dict) -> None:
        await self._http.request(self._original_response_route("PATCH", interaction), json=message)

    async def delete_status(self, interaction: Interaction) -> None:
        await self._http.request(self._original_response_route("DELETE", interaction))

    async def _send_poll(self, channel_id: int, question: str, issues: list[Issue]) -> None:
        poll = Poll(question=question, duration=POLL_DURATION, multiple=True)
        for issue in issues:
            poll.add_answer(text=_truncate(issue.title, POLL_ANSWER_MAX))
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
