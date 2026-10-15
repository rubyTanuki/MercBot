import asyncio
import logging
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import uvicorn
from discord import Forbidden
from discord.http import HTTPClient
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from MercBot import discordbot
from MercBot.discordbot import DiscordBot, InteractionVerifier
from MercBot.linear import LinearClient, LinearNotConnectedError
from MercBot.models import (
    AcceptRecord,
    Interaction,
    InteractionType,
    Issue,
    LinearProject,
    Pitch,
    ReviewDraft,
    ReviewOption,
    Settings,
    Workspace,
)
from MercBot.storage import AcceptRecordStore, KVStore, ReviewDraftStore

log = logging.getLogger("mercbot")

INTERACTIONS_PATH = "/mercbot"
LINEAR_CALLBACK_PATH = "/mercbot/linear/callback"

# Linear team keys: pitches become WRI issues, with VIS sub-issues for their photo/graphic tags.
WRI, VIS = "WRI", "VIS"
# Forum tag names, matched case-insensitively.
ACCEPTED_TAG, DEFERRED_TAG = "accepted", "accepted - postponed"
# How far back /accept looks for pitch threads, by creation date.
PITCH_WINDOW = timedelta(days=90)
# Command choices come from Linear; any command run re-syncs them in the background once they're this old.
COMMAND_SYNC_INTERVAL = timedelta(minutes=10)
LINEAR_REAPPROVE = "Ask a Linear admin to approve it again with `python -m MercBot.main linear-link`."


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        discord_application_id=os.environ["DISCORD_APPLICATION_ID"],
        discord_public_key=os.environ["DISCORD_PUBLIC_KEY"],
        discord_bot_token=os.environ["DISCORD_BOT_TOKEN"],
        # Comma-separated; commands are registered in every listed server.
        discord_guild_ids=[g.strip() for g in os.environ["DISCORD_GUILD_ID"].split(",") if g.strip()],
        pitches_channel_id=os.environ["PITCHES_CHAT_ID"],
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


def can_run_accept(interaction: Interaction) -> bool:
    # Any role may run /accept for now; see can_run_poll.
    return True


def can_run_create_project(interaction: Interaction) -> bool:
    # Any role may run /create-project for now; see can_run_poll.
    return True


DATE_FORMATS_WITH_YEAR = ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")
DATE_FORMATS_WITHOUT_YEAR = ("%B %d", "%b %d", "%m/%d")


def parse_issue_date(text: str, today: date) -> date | None:
    """Read dates like "November 9", "Nov. 9th", "11/9" or "2026-11-09". Without a year it's the next
    such date on or after `today`."""
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text.strip(), flags=re.IGNORECASE)
    text = " ".join(text.replace(",", " ").replace(".", " ").split())
    text = re.sub(r"\bsept\b", "sep", text, flags=re.IGNORECASE)
    for fmt in DATE_FORMATS_WITH_YEAR:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    # Parsed with an explicit year so Feb 29 isn't checked against strptime's default of 1900.
    for year in (today.year, today.year + 1):
        for fmt in DATE_FORMATS_WITHOUT_YEAR:
            try:
                parsed = datetime.strptime(f"{text} {year}", f"{fmt} %Y").date()
            except ValueError:
                continue
            if parsed >= today:
                return parsed
    return None


class CommandSync:
    """Keeps command choices (labels, upcoming projects, WRI sections) in step with Linear."""

    def __init__(self, bot: DiscordBot, linear: LinearClient, timezone: str):
        self._bot = bot
        self._linear = linear
        self._timezone = ZoneInfo(timezone)
        self._lock = asyncio.Lock()
        self._registered: list[dict] | None = None
        self._synced_at = float("-inf")

    def today(self) -> date:
        return datetime.now(self._timezone).date()

    def is_stale(self) -> bool:
        return time.monotonic() - self._synced_at > COMMAND_SYNC_INTERVAL.total_seconds()

    async def sync(self, *, force: bool = False) -> None:
        async with self._lock:
            try:
                labels = await self._linear.label_names()
                workspace = await self._linear.workspace()
            except LinearNotConnectedError:
                labels, workspace = [], Workspace(teams={}, projects=[])
            except Exception:
                log.exception("could not load Linear choices; commands will take free text")
                labels, workspace = [], Workspace(teams={}, projects=[])
            projects = [p.name for p in workspace.upcoming_projects(self.today())]
            sections = [label.name for label in workspace.teams[WRI].labels] if WRI in workspace.teams else []
            definitions = discordbot.commands(labels, projects, sections)
            # Only re-register on change, since every registration is a write against Discord's command limits.
            if force or definitions != self._registered:
                await self._bot.register_commands(definitions)
                self._registered = definitions
                log.info(
                    "registered commands in %d server(s): %d tags, %d dates, %d sections",
                    len(self._bot.guild_ids), len(labels), len(projects), len(sections),
                )
            self._synced_at = time.monotonic()

    async def sync_quietly(self) -> None:
        try:
            await self.sync()
        except Exception:
            log.exception("background command sync failed")


@dataclass
class Context:
    settings: Settings
    bot: DiscordBot
    linear: LinearClient
    drafts: ReviewDraftStore
    records: AcceptRecordStore
    sync: CommandSync


async def report_failure(ctx: Context, interaction: Interaction, message: str) -> None:
    try:
        await ctx.bot.edit_status(interaction, message)
    except Exception:
        log.exception("could not report failure to Discord")


async def show_review(ctx: Context, interaction: Interaction, draft: ReviewDraft, *, content: str, noun: str, confirm: str):
    draft_id = secrets.token_urlsafe(12)
    await ctx.drafts.save(draft_id, draft)
    message = discordbot.review_message(draft_id, draft, content=content, noun=noun, confirm_label=confirm)
    await ctx.bot.edit_original(interaction, message)


async def run_poll(ctx: Context, interaction: Interaction, tag: str) -> None:
    """Fetch the tagged issues and show the invoker a review message to pick which go in the poll."""
    try:
        issues = await ctx.linear.fetch_unassigned_issues(tag)
        if not issues:
            await ctx.bot.edit_status(interaction, f"No unassigned tasks tagged \"{tag}\" found.")
            return
        titles = [issue.title for issue in issues[: discordbot.MAX_REVIEW_OPTIONS]]
        draft = ReviewDraft(
            kind="poll",
            user_id=interaction.user_id,
            options=[ReviewOption(label=title) for title in titles],
            selected=list(range(len(titles))),
            tag=tag,
            channel_id=interaction.channel_id,
        )
        count = len(titles)
        content = (
            f"**{count} unassigned {tag} task{'s' if count != 1 else ''}"
            f"{f' (showing the first {count})' if len(issues) > count else ''}.** "
            "Untick any you don't want in the poll, then press **Post poll**."
        )
        await show_review(ctx, interaction, draft, content=content, noun="Tasks", confirm="Post poll")
    except LinearNotConnectedError:
        log.warning("/poll ran before Linear was connected; see the startup log for the connect link")
        await report_failure(ctx, interaction, "Linear isn't connected yet, so no tasks could be fetched.")
    except Exception:
        log.exception("/poll failed for tag %r", tag)
        await report_failure(ctx, interaction, "Something went wrong posting polls.")


async def post_reviewed_poll(ctx: Context, interaction: Interaction, draft: ReviewDraft) -> None:
    try:
        issues = [Issue(title=draft.options[i].label) for i in draft.selected]
        await ctx.bot.send_polls(draft.channel_id, draft.tag, issues)
        await ctx.bot.delete_status(interaction)
    except Exception:
        log.exception("posting reviewed poll failed for tag %r", draft.tag)
        await report_failure(ctx, interaction, "Something went wrong posting polls.")


def find_pitches(workspace: Workspace, threads: list[dict], tags: dict[str, str], done: set[int], team: str | None) -> list[Pitch]:
    """Accepted pitch threads without issues yet, newest first, with their tags mapped to Linear labels."""
    wri, vis = workspace.teams[WRI], workspace.teams.get(VIS)
    pitches = []
    for thread in sorted(threads, key=lambda t: int(t["id"]), reverse=True):
        if int(thread["id"]) in done:
            continue
        names = [tags[tag_id] for tag_id in thread.get("applied_tags", []) if tag_id in tags]
        folded = {name.casefold() for name in names}
        if ACCEPTED_TAG not in folded and DEFERRED_TAG not in folded:
            continue
        if team and team.casefold() not in folded:
            continue
        wri_labels = [wri.label(team).name] if team else [label.name for n in names if (label := wri.label(n))]
        vis_labels = [label.name for n in names if vis and (label := vis.label(n))]
        pitches.append(
            Pitch(
                thread_id=int(thread["id"]),
                title=thread["name"],
                wri_labels=wri_labels,
                vis_labels=vis_labels,
                deferred=ACCEPTED_TAG not in folded,
            )
        )
    return pitches


def pitch_summary(pitch: Pitch) -> str:
    parts = [" + ".join(pitch.wri_labels) or "No section tag"]
    if pitch.vis_labels:
        parts.append(" + ".join(pitch.vis_labels))
    if pitch.deferred:
        parts.append("deferred")
    return " · ".join(parts)


async def run_accept(ctx: Context, interaction: Interaction, project_name: str, team: str | None) -> None:
    """Gather accepted pitches without issues and show the invoker a review message to pick which to create."""
    try:
        if not await ctx.linear.can_create_issues():
            await ctx.bot.edit_status(interaction, f"MercBot only has read access to Linear, so it can't create issues. {LINEAR_REAPPROVE}")
            return
        workspace = await ctx.linear.workspace()
        project = workspace.project(project_name)
        if project is None:
            await ctx.bot.edit_status(interaction, f"There's no open Linear project called \"{project_name}\".")
            return
        if WRI not in workspace.teams:
            await ctx.bot.edit_status(interaction, f"Linear has no {WRI} team to put pitches in.")
            return
        if team and workspace.teams[WRI].label(team) is None:
            await ctx.bot.edit_status(interaction, f"\"{team}\" isn't a {WRI} label in Linear.")
            return

        channel_id = ctx.settings.pitches_channel_id
        guild_id, tags = await ctx.bot.forum(channel_id)
        threads = await ctx.bot.forum_threads(channel_id, guild_id, since=datetime.now(UTC) - PITCH_WINDOW)
        pitches = find_pitches(workspace, threads, tags, await ctx.records.thread_ids(), team)
        scope = f" tagged {team}" if team else ""
        if not pitches:
            await ctx.bot.edit_status(interaction, f"No accepted pitches{scope} are waiting for issues.")
            return

        shown = pitches[: discordbot.MAX_REVIEW_OPTIONS]
        draft = ReviewDraft(
            kind="accept",
            user_id=interaction.user_id,
            options=[ReviewOption(label=p.title, description=pitch_summary(p)) for p in shown],
            selected=[i for i, p in enumerate(shown) if not p.deferred],
            project=project.name,
            pitches=shown,
        )
        lines = [
            f"**{len(pitches)} accepted pitch{'es' if len(pitches) != 1 else ''}{scope} waiting for issues"
            f"{f' (showing the newest {len(shown)})' if len(pitches) > len(shown) else ''}.**",
            f"Ticked pitches become issues in **{project.name}**. Deferred ones start unticked; "
            "anything left unticked stays in the queue for a later /accept.",
        ]
        if any(p.vis_labels for p in shown) and VIS not in project.team_keys:
            lines.append(f"⚠️ {project.name} doesn't include the {VIS} team, so photo/graphic sub-issues won't be added to it.")
        await show_review(ctx, interaction, draft, content="\n".join(lines), noun="Pitches", confirm="Create issues")
    except LinearNotConnectedError:
        await report_failure(ctx, interaction, f"Linear isn't connected yet. {LINEAR_REAPPROVE}")
    except Forbidden:
        log.exception("/accept can't read the pitches forum")
        await report_failure(ctx, interaction, "MercBot can't see the pitches forum. Give its role View Channel there.")
    except Exception:
        log.exception("/accept failed for %r", project_name)
        await report_failure(ctx, interaction, "Something went wrong gathering pitches.")


async def create_pitch_issues(ctx: Context, workspace: Workspace, project: LinearProject, guild_id: int, pitch: Pitch) -> str:
    """Create a pitch's WRI issue and VIS sub-issues, record them, and link them in its thread. Returns a summary line."""
    if await ctx.records.get(pitch.thread_id):
        return f"• {pitch.title}: skipped, it was already handled"
    wri, vis = workspace.teams[WRI], workspace.teams.get(VIS)
    url = discordbot.thread_url(guild_id, pitch.thread_id)
    try:
        description = discordbot.pitch_description(await ctx.bot.starter_message(pitch.thread_id), url)
    except Exception:
        log.exception("could not read pitch %s", pitch.thread_id)
        return f"• ⚠️ {pitch.title}: couldn't read the Discord post, so nothing was created"
    try:
        main_issue = await ctx.linear.create_issue(
            team_id=wri.id,
            title=pitch.title,
            description=description,
            label_ids=[label.id for name in pitch.wri_labels if (label := wri.label(name))],
            project_id=project.id,
        )
    except Exception:
        log.exception("could not create issue for pitch %s", pitch.thread_id)
        return f"• ⚠️ {pitch.title}: Linear didn't create the issue, so nothing was created"

    # Recorded as soon as the main issue exists, so a failure below can't lead a re-run to duplicate it.
    record = AcceptRecord(project=project.name, issues=[main_issue])
    problems = []
    try:
        await ctx.records.save(pitch.thread_id, record)
    except Exception:
        log.exception("could not record issues for pitch %s", pitch.thread_id)
        problems.append("couldn't record it, so a later /accept may list it again")
    for name in pitch.vis_labels:
        label = vis.label(name) if vis else None
        if label is None:
            problems.append(f"no {VIS} label {name}")
            continue
        try:
            sub_issue = await ctx.linear.create_issue(
                team_id=vis.id,
                title=pitch.title,
                label_ids=[label.id],
                project_id=project.id if VIS in project.team_keys else None,
                parent_id=main_issue.id,
            )
            record.issues.append(sub_issue)
            await ctx.records.save(pitch.thread_id, record)
        except Exception:
            log.exception("could not create %s sub-issue for pitch %s", name, pitch.thread_id)
            problems.append(f"{name} sub-issue failed, create it by hand")

    links = discordbot.issue_links(record.issues)
    try:
        await ctx.bot.send_message(pitch.thread_id, f"Added to **{project.name}**: {links}")
    except Exception:
        log.exception("could not post issue links in pitch %s", pitch.thread_id)
        problems.append("couldn't post the links in the thread")
    line = f"• {pitch.title}: {links}"
    return line + (f" ⚠️ {'; '.join(problems)}" if problems else "")


async def create_accepted_issues(ctx: Context, interaction: Interaction, draft: ReviewDraft) -> None:
    try:
        workspace = await ctx.linear.workspace()
        project = workspace.project(draft.project)
        if project is None or WRI not in workspace.teams:
            await ctx.bot.edit_status(interaction, f"The Linear project \"{draft.project}\" or the {WRI} team is gone. Nothing was created.")
            return
        guild_id, _ = await ctx.bot.forum(ctx.settings.pitches_channel_id)
        pitches = [draft.pitches[i] for i in draft.selected]
        lines = []
        for n, pitch in enumerate(pitches, start=1):
            # Also strips the review components, so the buttons can't be pressed again mid-run.
            await ctx.bot.edit_status(interaction, f"Creating issues in **{project.name}**… {n}/{len(pitches)}")
            lines.append(await create_pitch_issues(ctx, workspace, project, guild_id, pitch))
        await ctx.bot.edit_status(interaction, f"**Created issues in {project.name}:**\n" + "\n".join(lines))
    except Exception:
        log.exception("/accept creation failed for %r", draft.project)
        await report_failure(ctx, interaction, "Something went wrong creating issues. Check Linear before running /accept again.")


async def run_create_project(ctx: Context, interaction: Interaction, text: str) -> None:
    """Create a newspaper issue's Linear project, shared by WRI and VIS, and offer it to /accept."""
    try:
        if not await ctx.linear.can_create_projects():
            await ctx.bot.edit_status(interaction, f"MercBot doesn't have write access to Linear, so it can't create projects. {LINEAR_REAPPROVE}")
            return
        when = parse_issue_date(text, ctx.sync.today())
        if when is None:
            await ctx.bot.edit_status(interaction, f"Couldn't read \"{text}\" as a date. Try something like November 9.")
            return
        name = f"{when:%B} {when.day}"
        workspace = await ctx.linear.workspace()
        missing = [key for key in (WRI, VIS) if key not in workspace.teams]
        if missing:
            await ctx.bot.edit_status(interaction, f"Linear has no {' or '.join(missing)} team.")
            return
        if existing := workspace.project(name):
            await ctx.bot.edit_status(interaction, f"There's already an open project called **{existing.name}**.")
            return
        project = await ctx.linear.create_project(
            name=name, team_ids=[workspace.teams[WRI].id, workspace.teams[VIS].id], target_date=when
        )
        # So the new date shows up in /accept right away.
        await ctx.sync.sync()
        await ctx.bot.edit_status(
            interaction,
            f"Created [{project.name}]({project.url}) for {WRI} and {VIS}, due {when:%A, %B} {when.day}, {when.year}. "
            "It's now a /accept date.",
        )
    except LinearNotConnectedError:
        await report_failure(ctx, interaction, f"Linear isn't connected yet. {LINEAR_REAPPROVE}")
    except Exception:
        log.exception("/create-project failed for %r", text)
        await report_failure(ctx, interaction, "Something went wrong creating the project. Check Linear before trying again.")


async def dismiss_review(ctx: Context, interaction: Interaction) -> None:
    try:
        await ctx.bot.delete_status(interaction)
    except Exception:
        log.exception("could not delete review message")


async def handle_review_component(ctx: Context, interaction: Interaction, background: BackgroundTasks) -> dict:
    parsed = discordbot.parse_review_id(interaction.data.custom_id or "")
    if parsed is None:
        raise HTTPException(status_code=400, detail="unknown component")
    action, draft_id, select_index = parsed

    draft = await ctx.drafts.get(draft_id)
    if draft is None:
        return discordbot.update_message("This selection expired. Run the command again.")
    if interaction.user_id != draft.user_id:
        return discordbot.ephemeral_message("Only the person who ran the command can change this.")

    if action == discordbot.REVIEW_SELECT:
        # Each select reports only its own ticks, so merge them into the draft's full selection.
        start = select_index * discordbot.SELECT_MAX_OPTIONS
        in_select = set(range(start, min(start + discordbot.SELECT_MAX_OPTIONS, len(draft.options))))
        ticked = {int(v) for v in interaction.data.values if v.isdigit()} & in_select
        draft.selected = sorted((set(draft.selected) - in_select) | ticked)
        await ctx.drafts.save(draft_id, draft)
        return discordbot.deferred_update()

    if action == discordbot.REVIEW_CONFIRM:
        if not draft.selected:
            return discordbot.ephemeral_message("Tick at least one first.")
        await ctx.drafts.delete(draft_id)
        run = post_reviewed_poll if draft.kind == "poll" else create_accepted_issues
        background.add_task(run, ctx, interaction, draft)
        return discordbot.deferred_update()

    await ctx.drafts.delete(draft_id)
    background.add_task(dismiss_review, ctx, interaction)
    return discordbot.deferred_update()


def string_option(interaction: Interaction, name: str) -> str:
    # Discord enforces choices and length limits client-side; re-check since these are still user input.
    value = str(interaction.data.option(name) or "").strip()
    return value if len(value) <= discordbot.OPTION_MAX_LENGTH else ""


def create_app(settings: Settings, linear: LinearClient, store: KVStore) -> FastAPI:
    verifier = InteractionVerifier(settings.discord_public_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = HTTPClient(asyncio.get_running_loop())
        await http.static_login(settings.discord_bot_token.get_secret_value())
        bot = DiscordBot(http, settings.discord_application_id, settings.discord_guild_ids)
        ctx = Context(
            settings=settings,
            bot=bot,
            linear=linear,
            drafts=ReviewDraftStore(store),
            records=AcceptRecordStore(store),
            sync=CommandSync(bot, linear, settings.timezone),
        )
        app.state.ctx = ctx
        await ctx.sync.sync(force=True)
        # Links are printed rather than served so only whoever can see the logs can start the install.
        if not await linear.is_connected():
            log.warning("Linear is not connected. Have a workspace admin open: %s", await linear.authorization_url())
        elif not await linear.can_create_projects():
            log.warning("Linear access lacks write, so /accept or /create-project can't run. Have a workspace admin open: %s",
                        await linear.authorization_url())
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

        ctx: Context = request.app.state.ctx
        interaction = Interaction.model_validate_json(body)
        if interaction.type == InteractionType.PING:
            return discordbot.pong()

        if interaction.type == InteractionType.MESSAGE_COMPONENT and interaction.data:
            return await handle_review_component(ctx, interaction, background)

        if interaction.type != InteractionType.APPLICATION_COMMAND or not interaction.data:
            raise HTTPException(status_code=400, detail="unsupported interaction")
        if ctx.sync.is_stale():
            background.add_task(ctx.sync.sync_quietly)

        # Background tasks run after the response is sent, keeping us inside Discord's 3s window.
        if interaction.data.name == discordbot.POLL_COMMAND:
            if not can_run_poll(interaction):
                return discordbot.ephemeral_message("You don't have permission to run /poll.")
            tag = string_option(interaction, discordbot.TAG_OPTION)
            if not tag:
                return discordbot.ephemeral_message(f"Give a tag of 1-{discordbot.OPTION_MAX_LENGTH} characters.")
            background.add_task(run_poll, ctx, interaction, tag)
            return discordbot.deferred_ephemeral()

        if interaction.data.name == discordbot.ACCEPT_COMMAND:
            if not can_run_accept(interaction):
                return discordbot.ephemeral_message("You don't have permission to run /accept.")
            project = string_option(interaction, discordbot.DATE_OPTION)
            if not project:
                return discordbot.ephemeral_message("Pick the newspaper issue to add the pitches to.")
            team = string_option(interaction, discordbot.TEAM_OPTION) or None
            background.add_task(run_accept, ctx, interaction, project, team)
            return discordbot.deferred_ephemeral()

        if interaction.data.name == discordbot.CREATE_PROJECT_COMMAND:
            if not can_run_create_project(interaction):
                return discordbot.ephemeral_message("You don't have permission to run /create-project.")
            text = string_option(interaction, discordbot.DATE_OPTION)
            if not text:
                return discordbot.ephemeral_message("Give the issue date, e.g. November 9.")
            background.add_task(run_create_project, ctx, interaction, text)
            return discordbot.deferred_ephemeral()

        raise HTTPException(status_code=400, detail="unsupported command")

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
        # Pick up the workspace's labels and projects as command choices without waiting for a restart.
        await app.state.ctx.sync.sync(force=True)
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

    app = create_app(settings, linear, store)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
