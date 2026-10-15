import datetime
import re

from discord import AllowedMentions, MessageFlags, Poll
from discord.http import HTTPClient, Route, handle_message_parameters
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from MercBot.models import CreatedIssue, Interaction, Issue, ReviewDraft

POLL_COMMAND = "poll"
TAG_OPTION = "tag"
ACCEPT_COMMAND = "accept"
DATE_OPTION = "date"
TEAM_OPTION = "team"
CREATE_PROJECT_COMMAND = "create-project"
OPTION_MAX_LENGTH = 100
MAX_CHOICES = 25


def _string_option(name: str, description: str, values: list[str], *, required: bool, keep_first: bool) -> dict:
    """A string option whose values become fixed choices, which Discord filters client-side with no round trip.
    Past Discord's 25-choice cap it keeps the first 25 (`keep_first`) or falls back to free text."""
    option = {"name": name, "description": description, "type": 3, "required": required}  # 3 = STRING
    choices = [value for value in values if len(value) <= OPTION_MAX_LENGTH]
    if keep_first:
        choices = choices[:MAX_CHOICES]
    if 0 < len(choices) <= MAX_CHOICES:
        option["choices"] = [{"name": value, "value": value} for value in choices]
    else:
        option.update(min_length=1, max_length=OPTION_MAX_LENGTH)
    return option


def commands(labels: list[str], projects: list[str], sections: list[str]) -> list[dict]:
    """Command definitions, with Linear labels, open projects (soonest first) and WRI section labels as choices."""
    return [
        {
            "name": POLL_COMMAND,
            "description": "Post a poll of unassigned Linear issues with a given tag",
            "type": 1,  # CHAT_INPUT
            "options": [_string_option(TAG_OPTION, "Linear label to filter by", labels, required=True, keep_first=False)],
        },
        {
            "name": ACCEPT_COMMAND,
            "description": "Create Linear issues from accepted pitches",
            "type": 1,
            "options": [
                # Discord requires required options before optional ones.
                _string_option(DATE_OPTION, "Newspaper issue to add the pitches to", projects, required=True, keep_first=True),
                _string_option(TEAM_OPTION, "Only pitches tagged with this section", sections, required=False, keep_first=False),
            ],
        },
        {
            "name": CREATE_PROJECT_COMMAND,
            "description": "Create a Linear project for a newspaper issue, shared by WRI and VIS",
            "type": 1,
            "options": [_string_option(DATE_OPTION, "Issue date, e.g. November 9", [], required=True, keep_first=False)],
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
# Messages full of Linear links would otherwise each get a preview card.
SUPPRESS_EMBEDS = 1 << 2
MESSAGE_MAX = 2000

# Interaction response types
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5
DEFERRED_UPDATE_MESSAGE = 6
UPDATE_MESSAGE = 7

# Review message components. custom_id is "<REVIEW_PREFIX>:<action>:<draft id>[:<select index>]".
REVIEW_PREFIX = "review"
REVIEW_SELECT = "select"
REVIEW_CONFIRM = "confirm"
REVIEW_CANCEL = "cancel"
SELECT_MAX_OPTIONS = 25
SELECT_TEXT_MAX = 100
# Discord allows 5 component rows; one holds the buttons.
MAX_REVIEW_OPTIONS = 4 * SELECT_MAX_OPTIONS

ACTION_ROW, BUTTON, STRING_SELECT = 1, 2, 3
BUTTON_SECONDARY, BUTTON_SUCCESS = 2, 3

DISCORD_EPOCH_MS = 1420070400000
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
CUSTOM_EMOJI_RE = re.compile(r"<a?(:\w+:)\d+>")


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


def review_message(draft_id: str, draft: ReviewDraft, *, content: str, noun: str, confirm_label: str) -> dict:
    """Ephemeral review step: ticked options go ahead when the invoker confirms; unticked ones are left out."""
    count = len(draft.options)
    rows = []
    for n, start in enumerate(range(0, count, SELECT_MAX_OPTIONS)):
        chunk = range(start, min(start + SELECT_MAX_OPTIONS, count))
        placeholder = noun if count <= SELECT_MAX_OPTIONS else f"{noun} {chunk.start + 1}-{chunk.stop}"
        options = []
        for i in chunk:
            option = {
                "label": truncate(draft.options[i].label, SELECT_TEXT_MAX),
                "value": str(i),
                "default": i in draft.selected,
            }
            if draft.options[i].description:
                option["description"] = truncate(draft.options[i].description, SELECT_TEXT_MAX)
            options.append(option)
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
        {"type": BUTTON, "style": BUTTON_SUCCESS, "label": confirm_label, "custom_id": f"{REVIEW_PREFIX}:{REVIEW_CONFIRM}:{draft_id}"},
        {"type": BUTTON, "style": BUTTON_SECONDARY, "label": "Cancel", "custom_id": f"{REVIEW_PREFIX}:{REVIEW_CANCEL}:{draft_id}"},
    ]
    rows.append({"type": ACTION_ROW, "components": buttons})
    return {"content": truncate(content, MESSAGE_MAX), "components": rows}


def parse_review_id(custom_id: str) -> tuple[str, str, int | None] | None:
    """(action, draft id, select index) for a review component's custom_id, or None if it isn't one."""
    parts = custom_id.split(":")
    if len(parts) == 3 and parts[0] == REVIEW_PREFIX and parts[1] in (REVIEW_CONFIRM, REVIEW_CANCEL):
        return parts[1], parts[2], None
    if len(parts) == 4 and parts[0] == REVIEW_PREFIX and parts[1] == REVIEW_SELECT and parts[3].isdigit():
        return parts[1], parts[2], int(parts[3])
    return None


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def issue_links(issues: list[CreatedIssue]) -> str:
    return " · ".join(f"[{issue.identifier}]({issue.url})" for issue in issues)


def snowflake_time(snowflake: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(((snowflake >> 22) + DISCORD_EPOCH_MS) / 1000, tz=datetime.UTC)


def thread_url(guild_id: int, thread_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def display_name(user: dict) -> str:
    return user.get("global_name") or user["username"]


def pitch_description(message: dict, url: str) -> str:
    """Markdown for a Linear issue from a pitch's starter message.

    Attachments are only counted, not linked: Discord's attachment URLs are signed and expire.
    """
    names = {user["id"]: display_name(user) for user in message.get("mentions", [])}
    body = USER_MENTION_RE.sub(lambda m: f"@{names.get(m.group(1), 'unknown user')}", message.get("content", ""))
    body = CUSTOM_EMOJI_RE.sub(r"\1", body).strip()
    parts = [body] if body else []
    if attachments := len(message.get("attachments", [])):
        parts.append(f"_{attachments} attachment{'s' if attachments != 1 else ''} on the Discord post._")
    parts.append(f"---\nPitched by {display_name(message['author'])} · [Discord thread]({url})")
    return "\n\n".join(parts)


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
    """REST-only Discord client: registers commands, reads the pitch forum and posts with the bot token."""

    def __init__(self, http: HTTPClient, application_id: int, guild_ids: list[int]):
        self._http = http
        self._application_id = application_id
        self.guild_ids = guild_ids

    async def register_commands(self, definitions: list[dict]) -> None:
        for guild_id in self.guild_ids:
            await self._http.bulk_upsert_guild_commands(self._application_id, guild_id, definitions)

    async def send_polls(self, channel_id: int, tag: str, issues: list[Issue]) -> None:
        # Discord caps polls at 10 answers, so larger batches are split across several polls.
        batches = [issues[i : i + ISSUES_PER_POLL] for i in range(0, len(issues), ISSUES_PER_POLL)]
        for n, batch in enumerate(batches, start=1):
            question = POLL_QUESTION.format(tag=tag)
            if len(batches) > 1:
                question += f" ({n}/{len(batches)})"
            await self._send_poll(channel_id, question, batch)

    async def forum(self, channel_id: int) -> tuple[int, dict[str, str]]:
        """The forum's guild id and its tag names by tag id."""
        channel = await self._http.get_channel(channel_id)
        return int(channel["guild_id"]), {tag["id"]: tag["name"] for tag in channel.get("available_tags", [])}

    async def forum_threads(self, channel_id: int, guild_id: int, since: datetime.datetime) -> list[dict]:
        """Open and archived posts in a forum created at or after `since`."""
        active = await self._http.get_active_threads(guild_id)
        threads = {t["id"]: t for t in active["threads"] if t.get("parent_id") == str(channel_id)}
        before = None
        while True:
            # Archived threads come newest-archived first; a post archived before `since` was created before it too.
            page = await self._http.get_public_archived_threads(channel_id, before=before, limit=100)
            threads.update({t["id"]: t for t in page["threads"]})
            if not page["threads"] or not page.get("has_more"):
                break
            before = min(t["thread_metadata"]["archive_timestamp"] for t in page["threads"])
            if datetime.datetime.fromisoformat(before) < since:
                break
        return [t for t in threads.values() if snowflake_time(int(t["id"])) >= since]

    async def starter_message(self, thread_id: int) -> dict:
        # A forum post's opening message shares the thread's id.
        return await self._http.get_message(thread_id, thread_id)

    async def send_message(self, channel_id: int, content: str) -> None:
        params = handle_message_parameters(
            content=truncate(content, MESSAGE_MAX),
            allowed_mentions=AllowedMentions.none(),
            flags=MessageFlags(suppress_embeds=True),
        )
        with params:
            await self._http.send_message(channel_id, params=params)

    async def edit_status(self, interaction: Interaction, content: str) -> None:
        """Replace the interaction's ephemeral message with plain `content`."""
        message = {"content": truncate(content, MESSAGE_MAX), "components": [], "flags": SUPPRESS_EMBEDS}
        await self.edit_original(interaction, message)

    async def edit_original(self, interaction: Interaction, message: dict) -> None:
        await self._http.request(self._original_response_route("PATCH", interaction), json=message)

    async def delete_status(self, interaction: Interaction) -> None:
        await self._http.request(self._original_response_route("DELETE", interaction))

    async def _send_poll(self, channel_id: int, question: str, issues: list[Issue]) -> None:
        poll = Poll(question=question, duration=POLL_DURATION, multiple=True)
        for issue in issues:
            poll.add_answer(text=truncate(issue.title, POLL_ANSWER_MAX))
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
