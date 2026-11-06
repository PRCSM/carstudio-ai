"""Supabase Storage over its REST API (private buckets, short-lived signed URLs)."""
import httpx

from app.core.config import Settings, get_settings


class StorageError(RuntimeError):
    pass


class SupabaseStorage:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.s = settings or get_settings()
        self.base = f"{self.s.supabase_url.rstrip('/')}/storage/v1"
        self.client = client or httpx.Client(timeout=60)

    @property
    def _headers(self) -> dict:
        key = self.s.supabase_service_role_key
        return {"Authorization": f"Bearer {key}", "apikey": key}

    def upload(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        r = self.client.post(f"{self.base}/object/{bucket}/{key}", content=data,
                             headers={**self._headers, "Content-Type": content_type, "x-upsert": "true"})
        if r.status_code >= 300:
            raise StorageError(f"upload failed: {r.status_code}")
        return key

    def download(self, bucket: str, key: str) -> bytes:
        r = self.client.get(f"{self.base}/object/{bucket}/{key}", headers=self._headers)
        if r.status_code >= 300:
            raise StorageError(f"download failed: {r.status_code}")
        return r.content

    def signed_url(self, bucket: str, key: str, ttl: int | None = None) -> str:
        r = self.client.post(f"{self.base}/object/sign/{bucket}/{key}", headers=self._headers,
                             json={"expiresIn": ttl or self.s.signed_url_ttl_seconds})
        if r.status_code >= 300:
            raise StorageError(f"sign failed: {r.status_code}")
        return f"{self.s.supabase_url.rstrip('/')}/storage/v1{r.json()['signedURL']}"

    def delete(self, bucket: str, keys: list[str]) -> None:
        if not keys:
            return
        r = self.client.request("DELETE", f"{self.base}/object/{bucket}", headers=self._headers,
                                json={"prefixes": keys})
        if r.status_code >= 300:
            raise StorageError(f"delete failed: {r.status_code}")


_storage: SupabaseStorage | None = None


def get_storage() -> SupabaseStorage:
    global _storage
    if _storage is None:
        _storage = SupabaseStorage()
    return _storage
