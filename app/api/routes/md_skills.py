from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi import File as UploadFileParam
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.core.errors import ApiError
from app.core.responses import success_response
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.kernel.md_skill_registry import scan_directory
from app.kernel.md_skill_watcher import MdSkillWatcher
from app.models import Skill, SkillVersion, User
from app.services.md_skill_upload import ingest_upload

router = APIRouter(prefix="/skills", tags=["md-skills"])


def out(request: Request, value):
    return success_response(request, value)


@router.post("/scan")
async def scan(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    result = scan_directory(db, tenant_id=user.tenant_id, root=get_settings().md_skills_dir, owner_user_id=user.id)
    db.commit()
    settings = get_settings()
    watcher = request.app.state.md_skill_watchers.get(str(user.tenant_id))
    if watcher is None:
        watcher = MdSkillWatcher(settings.md_skills_dir, user.tenant_id, interval_seconds=settings.md_skill_scan_interval, enabled=settings.md_skill_watch_enabled)
        request.app.state.md_skill_watchers[str(user.tenant_id)] = watcher
        await watcher.start()
    return out(request, {"discovered": result.discovered, "drafts_created": result.drafts_created, "unchanged": result.unchanged, "issues": [issue.__dict__ for issue in result.issues]})


@router.post("/upload")
async def upload_md_skill(request: Request, upload: UploadFile = UploadFileParam(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """上传 MD-Skill 技能包（zip 或 SKILL.md），解析 front matter 并创建 Skill + 草稿版本。"""
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in {".zip", ".md"}:
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "file type is not supported", 415)
    settings = get_settings()
    tmp_path = settings.file_storage_dir / f"md-skill-upload-{uuid4()}"
    try:
        size = 0
        with tmp_path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_size_mb * 1024 * 1024:
                    raise ApiError("AI_FILE_TOO_LARGE", "file exceeds configured limit", 413)
                handle.write(chunk)
        result = ingest_upload(db, tenant_id=user.tenant_id, owner_user_id=user.id, filename=upload.filename or "", tmp_path=tmp_path, trace_id=request.state.trace_id)
    finally:
        tmp_path.unlink(missing_ok=True)
    return out(request, result)


@router.get("/scan/status")
async def scan_status(request: Request, user: User = Depends(get_current_user)):
    watchers = getattr(request.app.state, "md_skill_watchers", {})
    watcher = watchers.get(str(user.tenant_id))
    if watcher is None:
        return out(request, {"running": False, "last_started_at": None, "last_finished_at": None, "last_result": None, "last_error": None})
    state = watcher.state
    return out(request, {"running": state.running, "last_started_at": state.last_started_at, "last_finished_at": state.last_finished_at, "last_error": state.last_error, "last_result": None if state.last_result is None else {"discovered": state.last_result.discovered, "drafts_created": state.last_result.drafts_created, "unchanged": state.last_result.unchanged, "issues": [issue.__dict__ for issue in state.last_result.issues]}})


@router.get("")
async def list_md_skills(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(Skill).where(Skill.tenant_id == user.tenant_id, Skill.skill_type == "MD_SKILL", Skill.status == "ACTIVE")).all()
    return out(request, [{"id": str(item.id), "name": item.name, "description": item.description, "skill_type": item.skill_type} for item in items])


@router.get("/{skill_id}/versions")
async def list_versions(skill_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    skill = db.scalar(select(Skill).where(Skill.id == skill_id, Skill.tenant_id == user.tenant_id, Skill.status == "ACTIVE"))
    if skill is None:
        raise ApiError("AI_SKILL_NOT_FOUND", "skill not found", 404)
    versions = db.scalars(select(SkillVersion).where(SkillVersion.skill_id == skill.id).order_by(SkillVersion.version_number)).all()
    return out(request, [{"id": str(item.id), "version_number": item.version_number, "status": item.status, "fingerprint": item.config_snapshot.get("file_fingerprint"), "snapshot_hash": item.snapshot_hash} for item in versions])


@router.get("/{skill_id}/versions/{version_id}")
async def get_version(skill_id: UUID, version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    skill = db.scalar(select(Skill).where(Skill.id == skill_id, Skill.tenant_id == user.tenant_id, Skill.status == "ACTIVE"))
    if skill is None:
        raise ApiError("AI_SKILL_NOT_FOUND", "skill not found", 404)
    version = db.scalar(select(SkillVersion).where(SkillVersion.id == version_id, SkillVersion.skill_id == skill.id))
    if version is None:
        raise ApiError("AI_SKILL_VERSION_NOT_FOUND", "skill version not found", 404)
    cfg = version.config_snapshot or {}
    return out(request, {
        "id": str(version.id),
        "version_number": version.version_number,
        "status": version.status,
        "implementation": cfg.get("implementation"),
        "declared_version": cfg.get("declared_version"),
        "instructions": version.instructions,
        "input_schema": cfg.get("input_schema"),
        "output_schema": cfg.get("output_schema"),
        "required_permissions": version.required_permissions,
        "script_files": cfg.get("script_files"),
        "allow_scripts": cfg.get("allow_scripts"),
        "file_fingerprint": cfg.get("file_fingerprint"),
        "relative_path": cfg.get("relative_path"),
        "snapshot_hash": version.snapshot_hash,
        "published_at": iso_utc(version.published_at),
    })
