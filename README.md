# MercBot

Discord bot that posts polls of unassigned Linear issues with a given label, so people can vote
on which tasks they want. `/poll tag:Photography` finds every open, unassigned issue labelled
`Photography` (matched case-insensitively) and first shows the invoker a private list with every
issue ticked; untick any to leave out, then press **Post poll**.

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

The bot serves Discord interactions at `POST /mercbot` on port 8787 and registers `/poll`
with the guild on every start.

## Connecting Linear

The bot reads Linear through an OAuth app installed in the workspace. This is a one-time step per
workspace: the resulting tokens are stored in Cloudflare KV and refreshed automatically, so restarts
and redeploys don't need it again. Redo it only if the app is revoked in Linear or the bot starts
reporting that Linear isn't connected.

1. Make sure the Linear OAuth app's callback URLs include
   `https://discord-dev.tostr.ai/mercbot/linear/callback`.
2. With `.env` pointing at the same KV namespace as the deployed bot, generate a link locally:

   ```sh
   cd src
   ../.venv/bin/python -m MercBot.main linear-link
   ```

3. Have a Linear workspace admin open the link within 10 minutes and approve. The deployed bot's
   callback finishes the install and shows "Linear connected."

The bot also logs a link on startup whenever no token is stored.
