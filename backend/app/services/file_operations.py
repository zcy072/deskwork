import asyncio
from dataclasses import dataclass
from typing import TypedDict

from app.config import settings
from app.sandbox import CONTAINER_MANAGER
from app.sandbox.quota import MB, QuotaExceededError, session_usage
from app.utils.filename import normalize_upload_filename, unique_upload_name


@dataclass
class InMemoryUpload:
    """A file whose bytes have already been read out of the request."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


class FileRecord(TypedDict):
    file_name: str
    container_path: str


async def upload_session_files(
    session_id: str,
    files: list[InMemoryUpload],
) -> list[FileRecord]:
    if not files:
        return []

    session = await CONTAINER_MANAGER.get_or_create(session_id)
    if settings.SESSION_MAX_STORAGE_MB:
        used = await asyncio.to_thread(session_usage, session.paths.session_root)
        incoming = sum(len(u.content) for u in files)
        if used + incoming > settings.SESSION_MAX_STORAGE_MB * MB:
            raise QuotaExceededError(
                f"会话存储配额不足:已用 {used // MB} MB,本次上传 {incoming // MB} MB,"
                f"上限 {settings.SESSION_MAX_STORAGE_MB} MB。请先删除不需要的文件或新建会话"
            )
    records: list[FileRecord] = []
    reserved_names: set[str] = set()

    for upload in files:
        filename = await asyncio.to_thread(
            unique_upload_name,
            session.paths.uploads,
            normalize_upload_filename(upload.filename or "upload"),
            reserved=reserved_names,
        )
        reserved_names.add(filename)
        await session.save_upload(filename, upload.content)
        records.append(
            {
                "file_name": filename,
                "container_path": f"{session.paths.virtual_uploads()}/{filename}",
            }
        )

    return records
