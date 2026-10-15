from datetime import datetime
from enum import IntEnum

from pydantic import BaseModel, SecretStr


class Settings(BaseModel):
    discord_application_id: int
    discord_public_key: str
    discord_bot_token: SecretStr
    discord_guild_id: int
    cloudflare_account_id: str
    cloudflare_kv_namespace_id: str
    cloudflare_api_token: SecretStr
    linear_client_id: str
    linear_client_secret: SecretStr
    public_base_url: str = "https://discord-dev.tostr.ai"
    host: str = "127.0.0.1"
    port: int = 8787


class Issue(BaseModel):
    """An unassigned Linear issue, as returned by the linear module.

    `title` is the parent issue's title for sub-issues, with any leading category prefix like
    "Photos - " stripped.
    """

    title: str


class LinearToken(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None


class InteractionType(IntEnum):
    PING = 1
    APPLICATION_COMMAND = 2


class InteractionUser(BaseModel):
    id: int


class InteractionMember(BaseModel):
    user: InteractionUser
    roles: list[int] = []


class InteractionOption(BaseModel):
    name: str
    value: str | int | float | bool | None = None


class InteractionData(BaseModel):
    name: str
    options: list[InteractionOption] = []

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
