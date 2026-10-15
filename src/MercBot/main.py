import asyncio
import logging
import os
import secrets
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
from MercBot.models import Interaction, InteractionType, Issue, PollDraft, Settings
from MercBot.storage import KVStore, PollDraftStore

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


async def run_poll(
    bot: DiscordBot, linear: LinearClient, drafts: PollDraftStore, interaction: Interaction, tag: str
) -> None:
    """Fetch the tagged issues and show the invoker a review message to pick which go in the poll."""
    try:
        issues = await linear.fetch_unassigned_issues(tag)
        if not issues:
            await bot.edit_status(interaction, f"No unassigned tasks tagged \"{tag}\" found.")
            return
        titles = [issue.title for issue in issues[: discordbot.MAX_REVIEW_ISSUES]]
        draft = PollDraft(
            tag=tag,
            channel_id=interaction.channel_id,
            user_id=interaction.user_id,
            titles=titles,
            selected=list(range(len(titles))),
        )
        draft_id = secrets.token_urlsafe(12)
        await drafts.save(draft_id, draft)
        truncated = len(issues) > len(titles)
        await bot.edit_original(interaction, discordbot.review_message(draft_id, draft, truncated))
    except LinearNotConnectedError:
        log.warning("/poll ran before Linear was connected; see the startup log for the connect link")
        await bot.edit_status(interaction, "Linear isn't connected yet, so no tasks could be fetched.")
    except Exception:
        log.exception("/poll failed for tag %r", tag)
        try:
            await bot.edit_status(interaction, "Something went wrong posting polls.")
        except Exception:
            log.exception("could not report /poll failure to Discord")


async def post_reviewed_poll(bot: DiscordBot, interaction: Interaction, draft: PollDraft) -> None:
    try:
        issues = [Issue(title=draft.titles[i]) for i in draft.selected]
        await bot.send_polls(draft.channel_id, draft.tag, issues)
        await bot.delete_status(interaction)
    except Exception:
        log.exception("posting reviewed poll failed for tag %r", draft.tag)
        try:
            await bot.edit_status(interaction, "Something went wrong posting polls.")
        except Exception:
            log.exception("could not report poll failure to Discord")


async def dismiss_review(bot: DiscordBot, interaction: Interaction) -> None:
    try:
        await bot.delete_status(interaction)
    except Exception:
        log.exception("could not delete review message")


async def handle_review_component(
    bot: DiscordBot, drafts: PollDraftStore, interaction: Interaction, background: BackgroundTasks
) -> dict:
    parsed = discordbot.parse_review_id(interaction.data.custom_id or "")
    if parsed is None:
        raise HTTPException(status_code=400, detail="unknown component")
    action, draft_id, select_index = parsed

    draft = await drafts.get(draft_id)
    if draft is None:
        return discordbot.update_message("This selection expired. Run /poll again.")
    if interaction.user_id != draft.user_id:
        return discordbot.ephemeral_message("Only the person who ran /poll can change this.")

    if action == discordbot.REVIEW_SELECT:
        # Each select reports only its own ticks, so merge them into the draft's full selection.
        start = select_index * discordbot.SELECT_MAX_OPTIONS
        in_select = set(range(start, min(start + discordbot.SELECT_MAX_OPTIONS, len(draft.titles))))
        ticked = {int(v) for v in interaction.data.values if v.isdigit()} & in_select
        draft.selected = sorted((set(draft.selected) - in_select) | ticked)
        await drafts.save(draft_id, draft)
        return discordbot.deferred_update()

    if action == discordbot.REVIEW_POST:
        if not draft.selected:
            return discordbot.ephemeral_message("Tick at least one task first.")
        await drafts.delete(draft_id)
        background.add_task(post_reviewed_poll, bot, interaction, draft)
        return discordbot.deferred_update()

    await drafts.delete(draft_id)
    background.add_task(dismiss_review, bot, interaction)
    return discordbot.deferred_update()


async def register_commands(bot: DiscordBot, linear: LinearClient) -> None:
    """Register /poll with the workspace's labels as tag choices, falling back to free text."""
    try:
        labels = await linear.label_names()
    except Exception:
        log.exception("could not load Linear labels; /poll will take a free-text tag")
        labels = []
    await bot.register_commands(labels)
    log.info("registered commands for guild %s with %d tag choices", bot.guild_id, len(labels))


def create_app(settings: Settings, linear: LinearClient, drafts: PollDraftStore) -> FastAPI:
    verifier = InteractionVerifier(settings.discord_public_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = HTTPClient(asyncio.get_running_loop())
        await http.static_login(settings.discord_bot_token.get_secret_value())
        bot = DiscordBot(http, settings.discord_application_id, settings.discord_guild_id)
        app.state.bot = bot
        if await linear.is_connected():
            await register_commands(bot, linear)
        else:
            await bot.register_commands([])
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

        if interaction.type == InteractionType.MESSAGE_COMPONENT and interaction.data:
            return await handle_review_component(request.app.state.bot, drafts, interaction, background)

        if interaction.data and interaction.data.name == discordbot.POLL_COMMAND:
            if not can_run_poll(interaction):
                return discordbot.ephemeral_message("You don't have permission to run /poll.")
            # Discord enforces the option's length limits client-side; re-check since this is user input.
            tag = str(interaction.data.option(discordbot.TAG_OPTION) or "").strip()
            if not tag or len(tag) > discordbot.TAG_MAX_LENGTH:
                return discordbot.ephemeral_message(f"Give a tag of 1-{discordbot.TAG_MAX_LENGTH} characters.")
            # Background tasks run after the response is sent, keeping us inside Discord's 3s window.
            background.add_task(run_poll, request.app.state.bot, linear, drafts, interaction, tag)
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
        # Pick up the workspace's labels as /poll choices without waiting for a restart.
        await register_commands(app.state.bot, linear)
        return PlainTextResponse("Linear connected. You can close this tab.")

    return app


def build_store(settings: Settings) -> KVStore:
    return KVStore(
        settings.cloudflare_account_id,
        settings.cloudflare_kv_namespace_id,
        settings.cloudflare_api_token.get_secret_value(),
    )


def build_linear(settings: Settings, store: KVStore) -> LinearClient:
    return LinearClient(
        settings.linear_client_id,
        settings.linear_client_secret.get_secret_value(),
        settings.public_base_url + LINEAR_CALLBACK_PATH,
        store,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    store = build_store(settings)
    linear = build_linear(settings, store)

    # `linear-link` only prints a fresh install link; the running bot's callback completes it via the KV-stored state.
    if sys.argv[1:] == ["linear-link"]:
        print(asyncio.run(linear.authorization_url()))
        return

    app = create_app(settings, linear, PollDraftStore(store))
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
