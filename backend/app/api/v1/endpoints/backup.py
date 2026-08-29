from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.deps import require_role
from app.models.user import User
from app.services.backup.service import create_backup, list_backups, resolve_backup_path

router = APIRouter()


class BackupOut(BaseModel):
    filename: str
    size_bytes: int
    created_at: str


def _out(info) -> BackupOut:
    return BackupOut(filename=info.filename, size_bytes=info.size_bytes, created_at=info.created_at.isoformat())


@router.get("", response_model=list[BackupOut])
async def list_backups_endpoint(_: User = Depends(require_role("administrator"))) -> list[BackupOut]:
    return [_out(b) for b in list_backups()]


@router.post("", response_model=BackupOut, status_code=status.HTTP_201_CREATED)
async def create_backup_endpoint(_: User = Depends(require_role("administrator"))) -> BackupOut:
    try:
        return _out(create_backup())
    except NotImplementedError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc


@router.get("/{filename}/download")
async def download_backup(filename: str, _: User = Depends(require_role("administrator"))) -> FileResponse:
    path = resolve_backup_path(filename)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)
