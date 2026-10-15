import asyncio
import re
import secrets
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

import requests

from MercBot import queries
from MercBot.models import CreatedIssue, CreatedProject, Issue, LinearLabel, LinearProject, LinearTeam, LinearToken, Workspace
from MercBot.storage import KVStore

AUTHORIZE_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
GRAPHQL_URL = "https://api.linear.app/graphql"

TOKEN_KEY = "linear_oauth"
# /poll only reads; /accept creates issues (sub-issues included); /create-project needs full write,
# since Linear has no narrower scope for projects.
SCOPES = "read,write"
CREATE_SCOPES = ("issues:create", "write")
# OAuth states live in KV rather than memory so any instance can finish an install another started.
STATE_KEY_PREFIX = "linear_oauth_state:"
STATE_TTL_SECONDS = 600
# Refresh a little early so a token can't expire between the check and the query.
REFRESH_MARGIN = timedelta(minutes=5)

# Titles are conventionally "<Category> - <name>", e.g. "Photos - Homecoming game"; polls only need the name.
CATEGORY_PREFIX_RE = re.compile(r"^\w+\s+-\s+")


def _display_title(node: dict) -> str:
    # Tasks are often sub-issues named just "Photos" under the article they're for; the article title is what voters need.
    title = node["parent"]["title"] if node.get("parent") else node["title"]
    return CATEGORY_PREFIX_RE.sub("", title).strip()


class LinearNotConnectedError(Exception):
    """No OAuth token is stored yet; a workspace admin needs to approve the app."""


class LinearClient:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, store: KVStore):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._store = store
        self._session = requests.Session()
        self._token: LinearToken | None = None
        self._token_lock = asyncio.Lock()

    async def authorization_url(self) -> str:
        """A one-time link, valid for 10 minutes, for a workspace admin to install the app (acting as itself)."""
        state = secrets.token_urlsafe(24)
        await self._store.set(STATE_KEY_PREFIX + state, "1", ttl_seconds=STATE_TTL_SECONDS)
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "actor": "app",
            "state": state,
            "prompt": "consent",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def is_connected(self) -> bool:
        async with self._token_lock:
            return await self._load_token() is not None

    async def connect(self, code: str, state: str) -> None:
        """Finish the OAuth flow started by `authorization_url`."""
        if await self._store.get(STATE_KEY_PREFIX + state) is None:
            raise ValueError("unknown or expired OAuth state")
        await self._store.delete(STATE_KEY_PREFIX + state)
        token = await asyncio.to_thread(
            self._request_token,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri},
        )
        async with self._token_lock:
            await self._save_token(token)

    async def fetch_unassigned_issues(self, label: str) -> list[Issue]:
        """Open, unassigned issues carrying `label` (matched case-insensitively by name)."""
        token = await self._valid_token()
        nodes = await asyncio.to_thread(self._query_issues, token.access_token, label)
        return [Issue(title=_display_title(node)) for node in nodes]

    async def can_create_issues(self) -> bool:
        """Whether the stored token was granted issue creation; installs from before /accept were read-only."""
        token = await self._valid_token()
        return any(token.has_scope(scope) for scope in CREATE_SCOPES)

    async def can_create_projects(self) -> bool:
        token = await self._valid_token()
        return token.has_scope("write")

    async def create_project(self, *, name: str, team_ids: list[str], target_date: date) -> CreatedProject:
        token = await self._valid_token()
        project_input = {"name": name, "teamIds": team_ids, "targetDate": target_date.isoformat()}
        data = await asyncio.to_thread(
            self._graphql, token.access_token, queries.CREATE_PROJECT, {"input": project_input}, 30
        )
        result = data["projectCreate"]
        if not result["success"] or not result["project"]:
            raise RuntimeError(f"Linear did not create project {name!r}")
        return CreatedProject(**result["project"])

    async def workspace(self) -> Workspace:
        """Teams with their labels, plus every project that isn't completed or canceled."""
        token = await self._valid_token()
        # Labels come from their own paginated query; nesting them under teams exceeds Linear's complexity limit.
        data = await asyncio.to_thread(self._graphql, token.access_token, queries.WORKSPACE, {}, 15)
        label_nodes = await asyncio.to_thread(self._query_labels, token.access_token)
        teams = {node["key"]: LinearTeam(id=node["id"], key=node["key"], labels=[]) for node in data["teams"]["nodes"]}
        for node in label_nodes:
            if node["isGroup"]:
                continue
            label = LinearLabel(id=node["id"], name=node["name"])
            # Workspace-level labels (no team) can go on any team's issues.
            for team in [teams[node["team"]["key"]]] if node["team"] else teams.values():
                team.labels.append(label)
        projects = [
            LinearProject(
                id=node["id"],
                name=node["name"],
                target_date=node["targetDate"],
                team_keys=[team["key"] for team in node["teams"]["nodes"]],
            )
            for node in data["projects"]["nodes"]
            if node["state"] not in ("completed", "canceled")
        ]
        projects.sort(key=lambda p: (p.target_date is None, p.target_date or date.max, p.name.casefold()))
        return Workspace(teams=teams, projects=projects)

    async def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str | None = None,
        label_ids: list[str] | None = None,
        project_id: str | None = None,
        parent_id: str | None = None,
    ) -> CreatedIssue:
        token = await self._valid_token()
        # Titles and descriptions come from Discord users, so they only ever travel as GraphQL variables.
        issue_input = {"teamId": team_id, "title": title, "labelIds": label_ids or []}
        if description:
            issue_input["description"] = description
        if project_id:
            issue_input["projectId"] = project_id
        if parent_id:
            issue_input["parentId"] = parent_id
        data = await asyncio.to_thread(
            self._graphql, token.access_token, queries.CREATE_ISSUE, {"input": issue_input}, 30
        )
        result = data["issueCreate"]
        if not result["success"] or not result["issue"]:
            raise RuntimeError(f"Linear did not create issue {title!r}")
        return CreatedIssue(**result["issue"])

    async def label_names(self) -> list[str]:
        """Sorted, de-duplicated names of labels that can be put on issues."""
        token = await self._valid_token()
        nodes = await asyncio.to_thread(self._query_labels, token.access_token)
        # Team-scoped labels can share a name; groups can't be applied to issues themselves.
        names = {node["name"].casefold(): node["name"] for node in nodes if not node["isGroup"]}
        return sorted(names.values(), key=str.casefold)

    async def _valid_token(self) -> LinearToken:
        async with self._token_lock:
            token = await self._load_token()
            if token is None:
                raise LinearNotConnectedError
            if token.expires_at and token.refresh_token and token.expires_at - REFRESH_MARGIN <= datetime.now(UTC):
                refreshed = await asyncio.to_thread(
                    self._request_token,
                    {"grant_type": "refresh_token", "refresh_token": token.refresh_token},
                )
                # A refresh keeps the grant's scopes, whether or not the response repeats them.
                refreshed.scope = refreshed.scope or token.scope
                token = refreshed
                await self._save_token(token)
            return token

    async def _load_token(self) -> LinearToken | None:
        if self._token is None:
            raw = await self._store.get(TOKEN_KEY)
            self._token = LinearToken.model_validate_json(raw) if raw else None
        return self._token

    async def _save_token(self, token: LinearToken) -> None:
        # Linear rotates refresh tokens, so the new one must be persisted before it's needed again.
        await self._store.set(TOKEN_KEY, token.model_dump_json())
        self._token = token

    def _request_token(self, grant: dict[str, str]) -> LinearToken:
        response = self._session.post(
            TOKEN_URL,
            data={**grant, "client_id": self._client_id, "client_secret": self._client_secret},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        expires_in = body.get("expires_in")
        scope = body.get("scope")
        return LinearToken(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None,
            scope=" ".join(scope) if isinstance(scope, list) else scope,
        )

    def _query_labels(self, access_token: str) -> list[dict]:
        nodes: list[dict] = []
        after = None
        while True:
            labels = self._graphql(access_token, queries.ISSUE_LABELS, {"after": after}, 15)["issueLabels"]
            nodes.extend(labels["nodes"])
            if not labels["pageInfo"]["hasNextPage"]:
                return nodes
            after = labels["pageInfo"]["endCursor"]

    def _graphql(self, access_token: str, query: str, variables: dict, timeout: float) -> dict:
        response = self._session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        if response.status_code == 400:
            # Linear reports invalid queries and permission problems as 400s with a GraphQL error body.
            raise RuntimeError(f"Linear GraphQL errors: {response.text[:1000]}")
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise RuntimeError(f"Linear GraphQL errors: {body['errors']}")
        return body["data"]

    def _query_issues(self, access_token: str, label: str) -> list[dict]:
        nodes: list[dict] = []
        after = None
        while True:
            # `label` is user input: it only ever travels as a GraphQL variable, never in the query text.
            variables = {"label": label, "after": after}
            issues = self._graphql(access_token, queries.UNASSIGNED_ISSUES_WITH_LABEL, variables, 15)["issues"]
            nodes.extend(issues["nodes"])
            if not issues["pageInfo"]["hasNextPage"]:
                return nodes
            after = issues["pageInfo"]["endCursor"]
