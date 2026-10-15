# MercBot command guide

MercBot turns accepted pitches from the **#pitches** forum into Linear issues, then helps each team
hand those tasks out with a poll. Everything happens through three slash commands in Discord. Type
`/` in any channel and pick the command from the list.

Every command first replies with a message **only you can see**. Nothing is created or posted until
you confirm.

## The workflow at a glance

1. **Before an issue's pitches are accepted:** someone runs `/create-project` for that newspaper
   issue date (once per issue).
2. **Writers** post pitches in #pitches.
3. **Section editors** tag the pitches they're taking as `Accepted` or `Accepted - Postponed`.
4. **Section editors** run `/accept` to turn their accepted pitches into Linear issues.
5. **Each team** runs `/poll` in its own channel to see who wants which task, then assigns them in
   Linear.

## Pitches: how a post should look

For a pitch to come through correctly, its forum post needs:

- **Title:** the article's working title, and nothing else. It becomes the Linear issue's title
  word for word, so leave out names, dates and numbering.
- **Body:** the pitch itself. It becomes the issue's description, along with the pitcher's name and
  a link back to the post.
- **Tags:**
  - its section: `News`, `Life and Arts`, `Sports` or `Opinion`;
  - `Photography` and/or `Graphics` if the story needs visuals.

Editors add the acceptance tags:

| Tag | Meaning |
| --- | --- |
| `Accepted` | Ready to become an issue. Starts **ticked** in `/accept`. |
| `Accepted - Postponed` | Accepted, but for a later issue. Starts **unticked** in `/accept`. |

Pitches without either tag are ignored.

## `/create-project` — set up a newspaper issue

```
/create-project date: November 9
```

Creates the Linear project for that issue, named like "November 9" and shared by the Writers and
Visuals teams. Run it once per newspaper issue, before anyone runs `/accept` for that date.

- Dates can be written as `November 9`, `Nov. 9th`, `11/9` or `2026-11-09`. Without a year, it
  picks the next upcoming one.
- If a project with that name already exists, it says so and creates nothing.
- The new date shows up in `/accept` straight away.

## `/accept` — turn accepted pitches into Linear issues

```
/accept date: November 9   team: News
```

- **`date`** (required): the newspaper issue the pitches go into. The list shows every upcoming
  issue; once an issue's date has passed, it drops off the list.
- **`team`** (optional): only show pitches tagged with this section. Leave it out to see every
  section.

You'll get a private list of every accepted pitch that doesn't have issues yet, newest first:

- `Accepted` pitches start **ticked**; `Accepted - Postponed` pitches start **unticked**.
- Open the dropdown to tick or untick pitches. Each one shows its section and visual tags.
- Press **Create issues** to create issues for the ticked pitches, or **Cancel** to stop.

For each ticked pitch, MercBot:

1. creates a **Writers** issue with the pitch's title, body and section label, in that newspaper
   issue's project;
2. creates a **Visuals** sub-issue with the same title for each `Photography` or `Graphics` tag;
3. replies in the pitch's thread with links to the new issues, so the writer can see where it went.

When it's done, your private message turns into a summary with a link to each issue. Anything that
went wrong for a pitch is marked with ⚠️ and explained.

**Unticked pitches aren't lost.** They stay in the list for the next `/accept`, so a postponed pitch
can be ticked when its issue comes around. A pitch that already has issues is never offered again,
so running `/accept` twice won't create duplicates.

## `/poll` — hand out tasks

```
/poll tag: Photography
```

Run this in your team's channel. It finds every open, **unassigned** Linear issue with that label
and shows you a private list with all of them ticked. Untick any you don't want to include, then
press **Post poll**.

MercBot posts a poll in the channel that runs for 24 hours. Each task is an answer, plus a final
"No article" option. Discord polls hold 10 answers, so longer lists are split into numbered polls. People can vote for as many tasks as they want. Once the votes are in, assign
the tasks in Linear yourself. The poll doesn't assign anything.

Visuals sub-issues appear under their article's title, so a photographer sees "Senate report", not
"Photos".

## Common messages

| Message | What to do |
| --- | --- |
| "This selection expired. Run the command again." | The private list only works for 15 minutes. Run the command again. |
| "Only the person who ran the command can change this." | Run the command yourself to get your own list. |
| "No accepted pitches … are waiting for issues." | Every accepted pitch (in that section) already has issues. Check the pitch tags if you expected more. |
| "No unassigned tasks tagged … found." | Every issue with that label is already assigned or closed. |
| "There's no open Linear project called …" | The issue's project doesn't exist yet or was closed. Run `/create-project` for it. |
| "There's already an open project called …" | The project already exists; nothing to do. |
| A date is missing from `/accept` | Its date has passed, or no one has run `/create-project` for it yet. |
| Anything about Linear access or connection | MercBot needs re-approving in Linear. Tell whoever maintains the bot. |
| "Something went wrong …" | Tell whoever maintains the bot. For `/accept`, check Linear before running it again, since some issues may have been created. |
