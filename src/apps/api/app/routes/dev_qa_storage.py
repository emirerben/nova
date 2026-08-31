"""Local-only media serving for deterministic editor QA fixtures."""

import ipaddress

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from app import storage
from app.config import settings

router = APIRouter()


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@router.get("/{object_path:path}", include_in_schema=False)
async def get_fixture_object(object_path: str, request: Request) -> FileResponse:
    if (
        not settings.e2e_fixtures
        or settings.storage_provider.strip().lower() != "local"
        or not _is_loopback_client(request)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        path = storage.local_object_path(object_path)
    except (RuntimeError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(path)
