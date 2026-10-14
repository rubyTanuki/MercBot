from datetime import date
from enum import IntEnum

from pydantic import BaseModel, HttpUrl, SecretStr


class Settings(BaseModel):
    discord_application_id: int
    discord_public_key: str
    discord_bot_token: SecretStr
    discord_guild_id: int
    host: str = "127.0.0.1"
    port: int = 8787


class PhotoIssue(BaseModel):
    """An unassigned Linear issue with a photo/graphic task, as returned by the linear module."""

    title: str
    url: HttpUrl
    due_date: date | None = None



class InteractionType(IntEnum):
    PING = 1
    APPLICATION_COMMAND = 2


class InteractionUser(BaseModel):
    id: int


class InteractionMember(BaseModel):
    user: InteractionUser
    roles: list[int] = []


class InteractionData(BaseModel):
    name: str


class Interaction(BaseModel):
    """The subset of a Discord interaction payload MercBot uses; other fields are ignored."""

    type: int  # compared against InteractionType; unknown types fall through to a 400
    application_id: int
    token: str
    guild_id: int | None = None
    channel_id: int | None = None
    member: InteractionMember | None = None
    data: InteractionData | None = None
