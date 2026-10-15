import asyncio
from urllib.parse import quote

import requests

from MercBot.models import AcceptRecord, ReviewDraft

KV_NAMESPACE_API = "https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}"
KV_API = KV_NAMESPACE_API + "/values/{key}"


class KVStore:
    """String key/value storage backed by Cloudflare Workers KV's REST API."""

    def __init__(self, account_id: str, namespace_id: str, api_token: str):
        self._account_id = account_id
        self._namespace_id = namespace_id
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_token}"

    async def get(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get, key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store `value`; with `ttl_seconds` (KV minimum is 60) the key expires on its own."""
        await asyncio.to_thread(self._set, key, value, ttl_seconds)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    async def keys(self, prefix: str) -> list[str]:
        """All key names starting with `prefix`. KV listings can lag writes by up to a minute."""
        return await asyncio.to_thread(self._keys, prefix)

    def _get(self, key: str) -> str | None:
        response = self._session.get(self._url(key), timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    def _set(self, key: str, value: str, ttl_seconds: int | None) -> None:
        params = {"expiration_ttl": ttl_seconds} if ttl_seconds else None
        self._session.put(self._url(key), data=value.encode(), params=params, timeout=10).raise_for_status()

    def _delete(self, key: str) -> None:
        response = self._session.delete(self._url(key), timeout=10)
        if response.status_code != 404:
            response.raise_for_status()

    def _keys(self, prefix: str) -> list[str]:
        url = KV_NAMESPACE_API.format(account_id=self._account_id, namespace_id=self._namespace_id) + "/keys"
        names: list[str] = []
        cursor = None
        while True:
            params = {"prefix": prefix, "limit": 1000, **({"cursor": cursor} if cursor else {})}
            response = self._session.get(url, params=params, timeout=10)
            response.raise_for_status()
            body = response.json()
            names.extend(item["name"] for item in body["result"])
            cursor = (body.get("result_info") or {}).get("cursor")
            if not cursor:
                return names

    def _url(self, key: str) -> str:
        return KV_API.format(account_id=self._account_id, namespace_id=self._namespace_id, key=quote(key, safe=""))


class ReviewDraftStore:
    """Review drafts in KV, so any instance can handle the review clicks. They expire with Discord's
    15-minute interaction token, after which the review message can't be edited anyway."""

    KEY_PREFIX = "review_draft:"
    TTL_SECONDS = 15 * 60

    def __init__(self, store: KVStore):
        self._store = store

    async def get(self, draft_id: str) -> ReviewDraft | None:
        raw = await self._store.get(self.KEY_PREFIX + draft_id)
        return ReviewDraft.model_validate_json(raw) if raw else None

    async def save(self, draft_id: str, draft: ReviewDraft) -> None:
        await self._store.set(self.KEY_PREFIX + draft_id, draft.model_dump_json(), ttl_seconds=self.TTL_SECONDS)

    async def delete(self, draft_id: str) -> None:
        await self._store.delete(self.KEY_PREFIX + draft_id)


class AcceptRecordStore:
    """Which pitch threads /accept has already made issues for, kept indefinitely."""

    KEY_PREFIX = "accepted_thread:"

    def __init__(self, store: KVStore):
        self._store = store

    async def thread_ids(self) -> set[int]:
        return {int(key.removeprefix(self.KEY_PREFIX)) for key in await self._store.keys(self.KEY_PREFIX)}

    async def get(self, thread_id: int) -> AcceptRecord | None:
        raw = await self._store.get(f"{self.KEY_PREFIX}{thread_id}")
        return AcceptRecord.model_validate_json(raw) if raw else None

    async def save(self, thread_id: int, record: AcceptRecord) -> None:
        await self._store.set(f"{self.KEY_PREFIX}{thread_id}", record.model_dump_json())
