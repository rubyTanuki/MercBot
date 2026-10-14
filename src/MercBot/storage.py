import asyncio
from urllib.parse import quote

import requests

KV_API = "https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"


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

    def _url(self, key: str) -> str:
        return KV_API.format(account_id=self._account_id, namespace_id=self._namespace_id, key=quote(key, safe=""))
