from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
from uuid import UUID
from xml.etree import ElementTree as ET

import httpx

from src.gateway.application.ports.object_storage import IVersionedObjectStorage, StagedObject, StoredObject
from src.gateway.domain.exceptions import ItemNotFoundException, StorageException, UploadTooLargeException


_SEGMENT = re.compile(r"\A[a-zA-Z0-9_-]+\Z")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _segment(value: object) -> str:
    segment = str(value)
    if not _SEGMENT.fullmatch(segment):
        raise StorageException("Invalid storage key segment.")
    return segment


def _check_key(storage_key: str) -> str:
    parts = storage_key.split("/")
    if len(parts) < 3 or parts[0] not in {"staging", "objects"}:
        raise StorageException("Invalid storage key.")
    if any(not _SEGMENT.fullmatch(part) for part in parts[1:]):
        raise StorageException("Invalid storage key.")
    return storage_key


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _error_code(payload: bytes) -> str | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    for child in root.iter():
        if _local_name(child.tag) == "Code" and child.text:
            return child.text.strip()
    return None


class S3CompatibleStorageAdapter(IVersionedObjectStorage):
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        session_token: str | None = None,
        prefix: str = "",
        client: httpx.AsyncClient | None = None,
        multipart_part_bytes: int = 8 * 1024 * 1024,
        timeout_seconds: float = 30.0,
    ) -> None:
        cleaned_endpoint = (endpoint or "").strip().rstrip("/")
        scheme = urlsplit(cleaned_endpoint).scheme.lower()
        if scheme not in {"http", "https"} or not urlsplit(cleaned_endpoint).netloc:
            raise ValueError("A valid HTTP(S) S3 endpoint URL is required.")
        if not (bucket or "").strip() or "/" in bucket:
            raise ValueError("A valid S3 bucket name is required.")
        if not (region or "").strip():
            raise ValueError("A valid S3 region is required.")
        if not (access_key or "").strip():
            raise ValueError("An S3 access key is required.")
        if not secret_key:
            raise ValueError("An S3 secret key is required.")
        if multipart_part_bytes < 5 * 1024 * 1024:
            raise ValueError("Multipart part size must be at least 5 MiB.")
        self._endpoint = cleaned_endpoint
        self._bucket = bucket.strip()
        self._region = region.strip()
        self._access_key = access_key.strip()
        self._secret_key = secret_key
        self._session_token = session_token or None
        self._prefix = (prefix or "").strip().strip("/")
        self._part_bytes = multipart_part_bytes
        self._client = client
        self._timeout_seconds = timeout_seconds

    def _full_key(self, storage_key: str) -> str:
        _check_key(storage_key)
        if self._prefix:
            return f"{self._prefix}/{storage_key}"
        return storage_key

    def _staging_key(self, space_id: str, upload_id: UUID) -> str:
        return f"staging/{_segment(space_id)}/{upload_id}"

    def _final_key(self, space_id: str, document_id: UUID, revision_id: UUID) -> str:
        return f"objects/{_segment(space_id)}/{document_id}/{revision_id}"

    def _sign(
        self,
        method: str,
        full_key: str | None,
        query: dict[str, str],
        extra_headers: dict[str, str],
        payload_hash: str,
        now: datetime,
    ) -> dict[str, str]:
        split = urlsplit(self._endpoint)
        host = split.netloc
        action_path = f"/{self._bucket}" if full_key is None else f"/{self._bucket}/{full_key}"
        canonical_uri = quote(action_path, safe="/~")
        canonical_query = "&".join(
            f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
            for name, value in sorted(query.items())
        )
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if self._session_token:
            headers["x-amz-security-token"] = self._session_token
        for name, value in extra_headers.items():
            headers[name.strip().lower()] = value.strip()
        ordered = sorted(headers.items())
        canonical_headers = "".join(f"{name}:{value}\n" for name, value in ordered)
        signed_headers = ";".join(name for name, _ in ordered)
        canonical_request = "\n".join(
            [method.upper(), canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{datestamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
        )
        signing_key = self._signing_key(datestamp)
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        signed = {
            "Authorization": authorization,
            "X-Amz-Date": amz_date,
            "X-Amz-Content-Sha256": payload_hash,
        }
        if self._session_token:
            signed["X-Amz-Security-Token"] = self._session_token
        for name, value in extra_headers.items():
            signed[name] = value
        return signed

    def _signing_key(self, datestamp: str) -> bytes:
        key = ("AWS4" + self._secret_key).encode()
        key = hmac.new(key, datestamp.encode(), hashlib.sha256).digest()
        key = hmac.new(key, self._region.encode(), hashlib.sha256).digest()
        key = hmac.new(key, b"s3", hashlib.sha256).digest()
        return hmac.new(key, b"aws4_request", hashlib.sha256).digest()

    def _url(self, full_key: str | None) -> str:
        if full_key is None:
            return f"{self._endpoint}/{self._bucket}"
        return f"{self._endpoint}/{self._bucket}/{quote(full_key, safe='/~')}"

    async def _request(
        self,
        method: str,
        full_key: str | None,
        *,
        query: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        body = content or b""
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = self._sign(
            method,
            full_key,
            query or {},
            extra_headers or {},
            payload_hash,
            datetime.now(timezone.utc),
        )
        owned = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds)
            owned = True
        try:
            return await client.request(
                method,
                self._url(full_key),
                params=query or {},
                headers=headers,
                content=body,
            )
        except httpx.HTTPError as exc:
            raise StorageException("The S3-compatible storage request failed.") from exc
        finally:
            if owned:
                await client.aclose()

    def _raise_for_status(self, response: httpx.Response, *, missing_ok: bool = False) -> None:
        if 200 <= response.status_code < 300:
            return
        code = _error_code(response.content)
        if code == "NoSuchBucket":
            raise StorageException("The configured S3 bucket does not exist.")
        if response.status_code == 404 or code in {"NoSuchKey", "NoSuchUpload", "NotFound"}:
            if missing_ok:
                return
            raise ItemNotFoundException()
        raise StorageException(f"The S3 request failed with status {response.status_code}.")

    async def stage(
        self,
        *,
        space_id: str,
        upload_id: UUID,
        chunks,
        max_bytes: int,
    ) -> StagedObject:
        if max_bytes <= 0:
            raise ValueError("A positive upload limit is required")
        key = self._staging_key(space_id, upload_id)
        full_key = self._full_key(key)
        head = await self._request("HEAD", full_key)
        if head.status_code == 200:
            raise StorageException("The staging object already exists.")
        if head.status_code != 404:
            self._raise_for_status(head)
        digest = hashlib.sha256()
        size = 0
        buffered = bytearray()
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise StorageException("Upload chunks must be bytes.")
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLargeException()
                digest.update(chunk)
                buffered += chunk
        except Exception:
            raise
        payload = bytes(buffered)
        if size > self._part_bytes:
            await self._multipart_put(full_key, payload)
        else:
            put = await self._request("PUT", full_key, content=payload)
            self._raise_for_status(put)
        return StagedObject(
            storage_key=key,
            size_bytes=size,
            checksum_sha256=digest.hexdigest(),
        )

    async def _multipart_put(self, full_key: str, payload: bytes) -> None:
        created = await self._request("POST", full_key, query={"uploads": ""})
        self._raise_for_status(created)
        try:
            root = ET.fromstring(created.content)
        except ET.ParseError as exc:
            raise StorageException("The S3 multipart upload could not be started.") from exc
        upload_id = None
        for child in root.iter():
            if _local_name(child.tag) == "UploadId" and child.text:
                upload_id = child.text.strip()
        if not upload_id:
            raise StorageException("The S3 multipart upload could not be started.")
        parts = [
            payload[index : index + self._part_bytes]
            for index in range(0, len(payload), self._part_bytes)
        ]
        etags: list[tuple[int, str]] = []
        try:
            for number, part in enumerate(parts, start=1):
                uploaded = await self._request(
                    "PUT",
                    full_key,
                    query={"partNumber": str(number), "uploadId": upload_id},
                    content=part,
                )
                self._raise_for_status(uploaded)
                etag = (uploaded.headers.get("ETag") or "").strip().strip('"')
                if not etag:
                    raise StorageException("The S3 multipart upload part was not acknowledged.")
                etags.append((number, etag))
            body = (
                "<CompleteMultipartUpload>"
                + "".join(
                    f"<Part><PartNumber>{number}</PartNumber><ETag>{etag}</ETag></Part>"
                    for number, etag in etags
                )
                + "</CompleteMultipartUpload>"
            ).encode()
            completed = await self._request(
                "POST",
                full_key,
                query={"uploadId": upload_id},
                content=body,
            )
            self._raise_for_status(completed)
        except Exception:
            aborted = await self._request(
                "DELETE", full_key, query={"uploadId": upload_id}
            )
            if aborted.status_code == 404:
                pass
            raise

    async def finalize(
        self,
        staging_key: str,
        *,
        space_id: str,
        document_id: UUID,
        revision_id: UUID,
    ) -> str:
        expected_prefix = f"staging/{_segment(space_id)}/"
        if not staging_key.startswith(expected_prefix):
            raise StorageException("The staging object does not belong to the requested space.")
        source = self._full_key(staging_key)
        final_key = self._final_key(space_id, document_id, revision_id)
        target = self._full_key(final_key)
        head = await self._request("HEAD", target)
        if head.status_code == 200:
            return final_key
        if head.status_code != 404:
            self._raise_for_status(head)
        copy_source = quote(f"/{self._bucket}/{source}", safe="/~")
        copied = await self._request(
            "PUT",
            target,
            extra_headers={"x-amz-copy-source": copy_source},
        )
        if copied.status_code == 404 and _error_code(copied.content) != "NoSuchBucket":
            raise ItemNotFoundException()
        self._raise_for_status(copied)
        deleted = await self._request("DELETE", source)
        if deleted.status_code != 404:
            self._raise_for_status(deleted)
        return final_key

    async def read(self, storage_key: str) -> bytes:
        response = await self._request("GET", self._full_key(storage_key))
        if response.status_code == 404 and _error_code(response.content) not in {"NoSuchBucket"}:
            raise ItemNotFoundException()
        self._raise_for_status(response)
        return response.content

    async def exists(self, storage_key: str) -> bool:
        response = await self._request("HEAD", self._full_key(storage_key))
        if response.status_code == 200:
            return True
        if response.status_code == 404 and _error_code(response.content) not in {"NoSuchBucket"}:
            return False
        self._raise_for_status(response)
        return False

    async def delete(self, storage_key: str) -> bool:
        response = await self._request("DELETE", self._full_key(storage_key))
        if response.status_code == 404 and _error_code(response.content) != "NoSuchBucket":
            return False
        self._raise_for_status(response)
        return True

    async def list_objects(self, prefix: str) -> tuple[StoredObject, ...]:
        if prefix not in {"staging", "objects"}:
            raise StorageException("Invalid storage inventory prefix.")
        logical_prefix = f"{self._prefix}/{prefix}/" if self._prefix else f"{prefix}/"
        found: list[StoredObject] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": logical_prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            response = await self._request("GET", None, query=query)
            self._raise_for_status(response)
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as exc:
                raise StorageException("The S3 object listing could not be parsed.") from exc
            truncated = False
            for child in root:
                name = _local_name(child.tag)
                if name == "Contents":
                    key_text = None
                    size_text = None
                    modified_text = None
                    for entry in child:
                        entry_name = _local_name(entry.tag)
                        if entry_name == "Key":
                            key_text = entry.text or ""
                        elif entry_name == "Size":
                            size_text = entry.text or "0"
                        elif entry_name == "LastModified":
                            modified_text = entry.text or ""
                    if not key_text or not modified_text:
                        raise StorageException("The S3 object listing could not be parsed.")
                    logical = key_text
                    if self._prefix:
                        if not key_text.startswith(f"{self._prefix}/"):
                            continue
                        logical = key_text[len(self._prefix) + 1 :]
                    _check_key(logical)
                    try:
                        modified_at = datetime.fromisoformat(modified_text.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise StorageException("The S3 object listing could not be parsed.") from exc
                    if modified_at.tzinfo is None:
                        modified_at = modified_at.replace(tzinfo=timezone.utc)
                    found.append(
                        StoredObject(
                            storage_key=logical,
                            size_bytes=int(size_text),
                            modified_at=modified_at,
                        )
                    )
                elif name == "IsTruncated" and (child.text or "").strip().lower() == "true":
                    truncated = True
                elif name == "NextContinuationToken" and child.text:
                    token = child.text.strip()
            if not truncated:
                break
            if not token:
                raise StorageException("The S3 object listing could not be parsed.")
        return tuple(sorted(found, key=lambda item: item.storage_key))

    async def aclose(self) -> None:
        if self._client is not None:
            return
