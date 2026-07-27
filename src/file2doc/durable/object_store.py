from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import BinaryIO, Protocol


MULTIPART_UPLOAD_THRESHOLD_BYTES = 20 * 1024 * 1024
MULTIPART_PART_SIZE_BYTES = 20 * 1024 * 1024


class ObjectStore(Protocol):
    def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None: ...

    def put_stream(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str,
        content_length: int,
    ) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def check_readiness(self) -> None: ...


@dataclass(frozen=True)
class LocalObjectStore:
    root: Path

    def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        del content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def put_stream(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str,
        content_length: int,
    ) -> None:
        del content_type, content_length
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def check_readiness(self) -> None:
        key = ".readiness/probe"
        self.put_bytes(key, b"ok", content_type="text/plain")
        try:
            if self.get_bytes(key) != b"ok":
                raise RuntimeError("object storage readiness probe could not be read")
        finally:
            self.delete(key)

    def _path(self, key: str) -> Path:
        normalized = key.strip("/")
        if not normalized:
            raise ValueError("object key must not be empty")
        path = (self.root / normalized).resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):
            raise ValueError("object key must stay within the object storage root")
        return path


@dataclass(frozen=True)
class TosObjectStore:
    bucket: str
    prefix: str
    client: object

    @classmethod
    def from_env(cls) -> TosObjectStore:
        import tos

        required = {
            "FILE2DOC_TOS_ENDPOINT": os.environ.get("FILE2DOC_TOS_ENDPOINT"),
            "FILE2DOC_TOS_REGION": os.environ.get("FILE2DOC_TOS_REGION"),
            "FILE2DOC_TOS_BUCKET": os.environ.get("FILE2DOC_TOS_BUCKET"),
            "FILE2DOC_TOS_ACCESS_KEY": os.environ.get("FILE2DOC_TOS_ACCESS_KEY"),
            "FILE2DOC_TOS_SECRET_KEY": os.environ.get("FILE2DOC_TOS_SECRET_KEY"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing TOS configuration: {', '.join(missing)}")

        client = tos.TosClientV2(
            required["FILE2DOC_TOS_ACCESS_KEY"],
            required["FILE2DOC_TOS_SECRET_KEY"],
            required["FILE2DOC_TOS_ENDPOINT"],
            required["FILE2DOC_TOS_REGION"],
            security_token=os.environ.get("FILE2DOC_TOS_SECURITY_TOKEN"),
            request_timeout=int(
                os.environ.get("FILE2DOC_TOS_REQUEST_TIMEOUT_SECONDS", "300")
            ),
            socket_timeout=int(
                os.environ.get("FILE2DOC_TOS_SOCKET_TIMEOUT_SECONDS", "300")
            ),
        )
        return cls(
            bucket=str(required["FILE2DOC_TOS_BUCKET"]),
            prefix=os.environ.get("FILE2DOC_TOS_PREFIX", "file2doc").strip("/"),
            client=client,
        )

    def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        self.client.put_object(
            self.bucket,
            self._key(key),
            content=content,
            content_type=content_type,
        )

    def put_stream(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str,
        content_length: int,
    ) -> None:
        if content_length >= MULTIPART_UPLOAD_THRESHOLD_BYTES:
            try:
                source_path = f"/proc/self/fd/{source.fileno()}"
            except (AttributeError, OSError, io.UnsupportedOperation):
                source_path = ""
            if source_path:
                checkpoint_fd, checkpoint_path = tempfile.mkstemp(
                    prefix="file2doc-tos-upload-",
                    suffix=".json",
                )
                os.close(checkpoint_fd)
                os.unlink(checkpoint_path)
                try:
                    self.client.upload_file(
                        self.bucket,
                        self._key(key),
                        source_path,
                        content_type=content_type,
                        part_size=MULTIPART_PART_SIZE_BYTES,
                        task_num=2,
                        enable_checkpoint=False,
                        checkpoint_file=checkpoint_path,
                    )
                finally:
                    Path(checkpoint_path).unlink(missing_ok=True)
                return
        self.client.put_object(
            self.bucket,
            self._key(key),
            content=source,
            content_length=content_length,
            content_type=content_type,
        )

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(self.bucket, self._key(key))
        return response.read()

    def delete(self, key: str) -> None:
        self.client.delete_object(self.bucket, self._key(key))

    def check_readiness(self) -> None:
        key = ".readiness/probe"
        self.put_bytes(key, b"ok", content_type="text/plain")
        try:
            if self.get_bytes(key) != b"ok":
                raise RuntimeError("TOS readiness probe could not be read")
        finally:
            self.delete(key)

    def _key(self, key: str) -> str:
        normalized = key.strip("/")
        if not normalized:
            raise ValueError("object key must not be empty")
        return f"{self.prefix}/{normalized}" if self.prefix else normalized


def object_store_from_env(*, local_root: Path | None = None) -> ObjectStore:
    backend = os.environ.get("FILE2DOC_OBJECT_STORE", "local").strip().lower()
    if backend == "tos":
        return TosObjectStore.from_env()
    if backend == "local":
        if local_root is None:
            configured = os.environ.get("FILE2DOC_STORAGE_ROOT", "/data/file2doc")
            local_root = Path(configured) / "objects"
        return LocalObjectStore(local_root)
    raise RuntimeError(f"Unsupported FILE2DOC_OBJECT_STORE backend: {backend}")
