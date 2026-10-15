from datetime import date, datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, SecretStr


class Settings(BaseModel):
    discord_application_id: int
    discord_public_key: str
    discord_bot_token: SecretStr
    discord_guild_ids: list[int]
    pitches_channel_id: int
    cloudflare_account_id: str
    cloudflare_kv_namespace_id: str
    cloudflare_api_token: SecretStr
    linear_client_id: str
    linear_client_secret: SecretStr
    public_base_url: str = "https://discord-dev.tostr.ai"
    # Decides when a newspaper issue's project stops being offered to /accept (after its target date here).
    timezone: str = "America/Chicago"
    host: str = "127.0.0.1"
    port: int = 8787


class Issue(BaseModel):
    """An unassigned Linear issue, as returned by the linear module.

    `title` is the parent issue's title for sub-issues, with any leading category prefix like
    "Photos - " stripped.
    """

    title: str


class LinearLabel(BaseModel):
    id: str
    name: str


class LinearTeam(BaseModel):
    id: str
    key: str
    labels: list[LinearLabel]

    def label(self, name: str) -> LinearLabel | None:
        return next((label for label in self.labels if label.name.casefold() == name.casefold()), None)


class LinearProject(BaseModel):
    id: str
    name: str
    target_date: date | None
    team_keys: list[str]


class Workspace(BaseModel):
    """The Linear teams, labels and open projects /accept needs, looked up by name."""

    teams: dict[str, LinearTeam]  # by team key, e.g. "WRI"
    projects: list[LinearProject]  # not completed/canceled, soonest target date first, undated last

    def project(self, name: str) -> LinearProject | None:
        return next((p for p in self.projects if p.name.casefold() == name.casefold()), None)

    def upcoming_projects(self, today: date) -> list[LinearProject]:
        """Projects /accept offers: ones whose target date hasn't passed. Undated ones can't be past, so stay."""
        return [p for p in self.projects if p.target_date is None or p.target_date >= today]


class CreatedIssue(BaseModel):
    id: str
    identifier: str
    url: str


class Pitch(BaseModel):
    """An accepted forum thread, with its tags already split into the Linear labels they map to."""

    thread_id: int
    title: str
    wri_labels: list[str]
    vis_labels: list[str]
    deferred: bool


class AcceptRecord(BaseModel):
    """What /accept created for a thread; its presence in KV is what stops a second run duplicating it.
    Pitches marked handled when /accept was adopted have no project or issues."""

    project: str | None
    issues: list[CreatedIssue]


class ReviewOption(BaseModel):
    label: str
    description: str | None = None


class ReviewDraft(BaseModel):
    """A command awaiting review: the options shown and which of them the invoker still has ticked."""

    kind: Literal["poll", "accept"]
    user_id: int
    options: list[ReviewOption]
    selected: list[int]
    # /poll
    tag: str | None = None
    channel_id: int | None = None
    # /accept
    project: str | None = None
    pitches: list[Pitch] = []


class LinearToken(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    # As Linear returns it (space- or comma-separated); None for tokens stored before it was recorded.
    scope: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scope or "").replace(",", " ").split()


class InteractionType(IntEnum):
    PING = 1
    APPLICATION_COMMAND = 2
    MESSAGE_COMPONENT = 3


class InteractionUser(BaseModel):
    id: int


class InteractionMember(BaseModel):
    user: InteractionUser
    roles: list[int] = []


class InteractionOption(BaseModel):
    name: str
    value: str | int | float | bool | None = None


class InteractionData(BaseModel):
    name: str | None = None  # set for commands
    options: list[InteractionOption] = []
    custom_id: str | None = None  # set for message components
    values: list[str] = []  # selected option values for select menus

    def option(self, name: str) -> str | int | float | bool | None:
        return next((o.value for o in self.options if o.name == name), None)


class Interaction(BaseModel):
    """The subset of a Discord interaction payload MercBot uses; other fields are ignored."""

    type: int  # compared against InteractionType; unknown types fall through to a 400
    application_id: int
    token: str
    guild_id: int | None = None
    channel_id: int | None = None
    member: InteractionMember | None = None
    data: InteractionData | None = None

    @property
    def user_id(self) -> int | None:
        return self.member.user.id if self.member else None
