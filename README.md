# MercBot

Discord bot that turns accepted pitches into Linear issues and polls each team on who takes them.

Using the bot as an editor? See the [command guide](docs/command-usage.md).

## Workflow

0. For each newspaper issue, someone runs `/create-project date:November 9`, which creates a Linear
   project named "November 9" with that target date, shared by the WRI and VIS teams. Dates like
   "Nov. 9th", "11/9" and "2026-11-09" work too; without a year it's the next upcoming one.
1. Writers pitch in the pitches forum: the post title is the article's working title, tagged with its
   section (News, Life and Arts, Sports, Opinion, Comics) and `Photography` / `Graphics` if it needs visuals.
2. Editors tag pitches `Accepted` or `Accepted - Postponed`.
3. An editor runs `/accept date:<newspaper issue> [team:<section>]`, reviews the private list
   (accepted pitches start ticked, postponed ones unticked) and presses **Create issues**. Each ticked
   pitch becomes a main issue (in its section's team) in that newspaper issue's Linear project, with the post as its
   description, plus a VIS sub-issue for each visual tag. The thread gets a reply linking them.
   Unticked pitches stay queued for a later `/accept`; pitches that already have issues are never
   offered again.
4. Each team runs `/poll tag:<label>`, reviews the private list of unassigned issues with that label
   (all ticked), and presses **Post poll** to let people vote on the ones they want.

`date` offers Linear projects that aren't completed and whose target date hasn't passed; `team` offers
WRI's labels; `tag` offers every Linear label. Forum tags must match Linear label names
(case-insensitively). `/accept` looks at pitch threads created in the last 90 days.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in the values
```

## Running

```sh
cd src
../.venv/bin/python -m MercBot.main
```

The bot serves Discord interactions at `POST /mercbot` on port 8787 and registers `/poll` and
`/accept` in every server listed in `DISCORD_GUILD_ID` (comma-separated) on start. Command choices
come from Linear and are re-synced in the background when a command runs and they're over 10 minutes
old. With more than 25 labels (Discord's limit) or Linear unreachable, `tag` falls back to free text.

Which labels count as sections or visuals (`ACCEPT_SECTIONS`, `ACCEPT_VISUALS`), team keys, forum tag
names, the pitch window and the timezone are optional settings in `.env`;
see `.env.example` for them and their defaults.

## Deploying

See [docs/deploy-ubuntu.md](docs/deploy-ubuntu.md) for running it on an Ubuntu server.

## Connecting Linear

The bot reads Linear and creates issues through an OAuth app installed in the workspace (scopes
`read` and `write`; Linear has no narrower scope for creating projects). This is a one-time step per workspace: the resulting tokens are stored
in Cloudflare KV and refreshed automatically, so restarts and redeploys don't need it again. Redo it
only if the app is revoked in Linear or the bot reports that Linear isn't connected or only has read
access.

1. Make sure the Linear OAuth app's callback URLs include
   `<PUBLIC_BASE_URL>/mercbot/linear/callback` (e.g. `https://discord-dev.tostr.ai/mercbot/linear/callback`).
2. With `.env` pointing at the same KV namespace as the deployed bot, generate a link locally:

   ```sh
   cd src
   ../.venv/bin/python -m MercBot.main linear-link
   ```

3. Have a Linear workspace admin open the link within 10 minutes and approve. The deployed bot's
   callback finishes the install and shows "Linear connected."

The bot also logs a link on startup whenever no token is stored or it only has read access.
