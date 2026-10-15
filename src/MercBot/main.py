import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from discord.http import HTTPClient
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from MercBot import discordbot
from MercBot.discordbot import DiscordBot, InteractionVerifier
from MercBot.linear import LinearClient, LinearNotConnectedError
from MercBot.models import Interaction, InteractionType, Settings
from MercBot.storage import KVStore

log = logging.getLogger("mercbot")

INTERACTIONS_PATH = "/mercbot"
LINEAR_CALLBACK_PATH = "/mercbot/linear/callback"


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        discord_application_id=os.environ["DISCORD_APPLICATION_ID"],
        discord_public_key=os.environ["DISCORD_PUBLIC_KEY"],
        discord_bot_token=os.environ["DISCORD_BOT_TOKEN"],
        discord_guild_id=os.environ["DISCORD_GUILD_ID"],
        cloudflare_account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        cloudflare_kv_namespace_id=os.environ["CLOUDFLARE_KV_NAMESPACE_ID"],
        cloudflare_api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        linear_client_id=os.environ["LINEAR_CLIENT_ID"],
        linear_client_secret=os.environ["LINEAR_CLIENT_SECRET"],
    )


def can_run_poll(interaction: Interaction) -> bool:
    # Any role may run /poll for now; role-based restrictions go here
    # (interaction.member.roles holds the invoker's role ids).
    return True


async def run_poll(bot: DiscordBot, linear: LinearClient, interaction: Interaction, tag: str) -> None:
    try:
        issues = await linear.fetch_unassigned_issues(tag)
        await bot.post_polls(interaction, tag, issues)
    except LinearNotConnectedError:
        log.warning("/poll ran before Linear was connected; see the startup log for the connect link")
        await bot.edit_status(interaction, "Linear isn't connected yet, so no tasks could be fetched.")
    except Exception:
        log.exception("/poll failed for tag %r", tag)
        try:
            await bot.edit_status(interaction, "Something went wrong posting polls.")
        except Exception:
            log.exception("could not report /poll failure to Discord")


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
        if not await linear.is_connected():
            # Printed rather than served so only whoever can see the logs can start the install.
            log.warning("Linear is not connected. Have a workspace admin open: %s", await linear.authorization_url())
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

        if interaction.data and interaction.data.name == discordbot.POLL_COMMAND:
            if not can_run_poll(interaction):
                return discordbot.ephemeral_message("You don't have permission to run /poll.")
            # Discord enforces the option's length limits client-side; re-check since this is user input.
            tag = str(interaction.data.option(discordbot.TAG_OPTION) or "").strip()
            if not tag or len(tag) > discordbot.TAG_MAX_LENGTH:
                return discordbot.ephemeral_message(f"Give a tag of 1-{discordbot.TAG_MAX_LENGTH} characters.")
            # Background tasks run after the response is sent, keeping us inside Discord's 3s window.
            background.add_task(run_poll, request.app.state.bot, linear, interaction, tag)
            return discordbot.deferred_ephemeral()

        raise HTTPException(status_code=400, detail="unsupported interaction")

    @app.get(LINEAR_CALLBACK_PATH)
    async def linear_callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if error or not code or not state:
            return PlainTextResponse(f"Linear authorization failed: {error or 'missing code'}", status_code=400)
        try:
            await linear.connect(code, state)
        except ValueError:
            return PlainTextResponse(
                "This link has expired or was already used. Generate a new one with "
                "`python -m MercBot.main linear-link`.",
                status_code=400,
            )
        log.info("Linear connected")
        return PlainTextResponse("Linear connected. You can close this tab.")

    return app


def build_linear(settings: Settings) -> LinearClient:
    store = KVStore(
        settings.cloudflare_account_id,
        settings.cloudflare_kv_namespace_id,
        settings.cloudflare_api_token.get_secret_value(),
    )
    return LinearClient(
        settings.linear_client_id,
        settings.linear_client_secret.get_secret_value(),
        settings.public_base_url + LINEAR_CALLBACK_PATH,
        store,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    linear = build_linear(settings)

    # `linear-link` only prints a fresh install link; the running bot's callback completes it via the KV-stored state.
    if sys.argv[1:] == ["linear-link"]:
        print(asyncio.run(linear.authorization_url()))
        return

    app = create_app(settings, linear)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
