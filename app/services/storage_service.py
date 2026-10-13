"""Receipt image storage.

Local disk by default (RECEIPTS_DIR, mounted as a Docker volume so images survive
container rebuilds). The StorageProvider interface means S3/GCS/Azure can be
swapped in later without touching the rest of the app.

The database stores only the *file name* (the reference), so the folder can move.
"""
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from app.config.settings import settings


class StorageProvider(ABC):
    @abstractmethod
    def store(self, file_content: bytes, extension: str) -> str:
        """Store content, return a reference."""

    @abstractmethod
    def retrieve(self, reference: str) -> Optional[bytes]:
        ...

    @abstractmethod
    def get_path(self, reference: str) -> Optional[str]:
        """Local filesystem path if the file exists, else None."""

    @abstractmethod
    def delete(self, reference: str) -> None:
        ...


class LocalStorageProvider(StorageProvider):
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path(base_dir or settings.RECEIPTS_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, reference: str) -> Path:
        # Only the file name is used: legacy rows stored "receipts/<uuid>.jpg" and a
        # tampered reference like "../../etc/passwd" can never escape base_dir.
        return self.base_dir / Path(reference).name

    def store(self, file_content: bytes, extension: str = ".jpg") -> str:
        extension = extension if extension.startswith(".") else f".{extension}"
        name = f"{uuid.uuid4().hex}{extension.lower()}"
        (self.base_dir / name).write_bytes(file_content)
        return name

    def retrieve(self, reference: str) -> Optional[bytes]:
        path = self._resolve(reference)
        return path.read_bytes() if path.is_file() else None

    def get_path(self, reference: str) -> Optional[str]:
        path = self._resolve(reference)
        return str(path) if path.is_file() else None

    def delete(self, reference: str) -> None:
        path = self._resolve(reference)
        if path.is_file():
            path.unlink()


class StorageService:
    def __init__(self, provider: Optional[StorageProvider] = None):
        self.provider = provider or LocalStorageProvider()

    async def save_file(self, file_content: bytes, extension: str = ".jpg") -> str:
        return self.provider.store(file_content, extension)

    async def get_file(self, reference: str) -> Optional[bytes]:
        return self.provider.retrieve(reference)

    async def get_file_path(self, reference: str) -> Optional[str]:
        return self.provider.get_path(reference)

    async def delete_file(self, reference: str) -> None:
        self.provider.delete(reference)


storage_service = StorageService()
