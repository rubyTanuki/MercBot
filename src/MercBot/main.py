import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from discord.http import HTTPClient
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from MercBot import discordbot
from MercBot.discordbot import DiscordBot, InteractionVerifier
from MercBot.linear import LinearClient
from MercBot.models import Interaction, InteractionType, Settings

log = logging.getLogger("mercbot")

INTERACTIONS_PATH = "/mercbot"


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        discord_application_id=os.environ["DISCORD_APPLICATION_ID"],
        discord_public_key=os.environ["DISCORD_PUBLIC_KEY"],
        discord_bot_token=os.environ["DISCORD_BOT_TOKEN"],
        discord_guild_id=os.environ["DISCORD_GUILD_ID"],
    )


def can_run_photo_poll(interaction: Interaction) -> bool:
    # Any role may run /photo-poll for now; role-based restrictions go here
    # (interaction.member.roles holds the invoker's role ids).
    return True


async def run_photo_poll(bot: DiscordBot, linear: LinearClient, interaction: Interaction) -> None:
    try:
        issues = await linear.fetch_unassigned_photo_issues()
        await bot.post_photo_polls(interaction, issues)
    except Exception:
        log.exception("photo-poll failed")
        try:
            await bot.edit_status(interaction, "Something went wrong posting photo polls.")
        except Exception:
            log.exception("could not report photo-poll failure to Discord")


def create_app(settings: Settings, linear: LinearClient) -> FastAPI:
    verifier = InteractionVerifier(settings.discord_public_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = HTTPClient(asyncio.get_running_loop())
        await http.static_login(settings.discord_bot_token.get_secret_value())
        bot = DiscordBot(http, settings.discord_application_id, settings.discord_guild_id)
        await bot.register_commands()
        log.info("registered commands for guild %s", settings.discord_guild_id)
        app.state.bot = bot
        try:
            yield
        finally:
            await http.close()

    app = FastAPI(lifespan=lifespan)

    @app.post(INTERACTIONS_PATH)
    async def interactions(request: Request, background: BackgroundTasks) -> dict:
        body = await request.body()
        signature = request.headers.get("X-Signature-Ed25519")
        timestamp = request.headers.get("X-Signature-Timestamp")
        if not signature or not timestamp or not verifier.verify(signature, timestamp, body):
            raise HTTPException(status_code=401, detail="invalid request signature")

        interaction = Interaction.model_validate_json(body)
        if interaction.type == InteractionType.PING:
            return discordbot.pong()

        if interaction.data and interaction.data.name == discordbot.PHOTO_POLL_COMMAND:
            if not can_run_photo_poll(interaction):
                return discordbot.ephemeral_message("You don't have permission to run /photo-poll.")
            # Background tasks run after the response is sent, keeping us inside Discord's 3s window.
            background.add_task(run_photo_poll, request.app.state.bot, linear, interaction)
            return discordbot.deferred_ephemeral()

        raise HTTPException(status_code=400, detail="unsupported interaction")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    app = create_app(settings, LinearClient())
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
