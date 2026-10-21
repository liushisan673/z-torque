from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.kernel.md_skill import _ALLOWED, _NAME, normalize_newlines, parse_skill
from app.kernel.md_skill_registry import register_skill_dir
from app.models import Skill

_ZIP_MAGIC = b"PK\x03\x04"


def peek_skill_name(md_text: str) -> str:
    """从 SKILL.md 的 YAML front matter 提取 name（仅用于确定落盘目录名，完整校验仍由 parse_skill 负责）。"""
    md_text = normalize_newlines(md_text)
    if not md_text.startswith("---\n"):
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "YAML front matter is required", 422)
    marker = md_text.find("\n---\n", 4)
    if marker < 0:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "YAML front matter is not closed", 422)
    try:
        meta = yaml.safe_load(md_text[4:marker]) or {}
    except yaml.YAMLError as exc:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "YAML front matter is invalid", 422) from exc
    name = meta.get("name") if isinstance(meta, dict) else None
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "skill name is invalid", 422)
    return name


def _extract_zip(zip_path: Path, stage_root: Path) -> None:
    """安全解压到 staging：写入前逐条目校验，防 zip slip / zip bomb / 非法后缀。"""
    settings = get_settings()
    max_total_bytes = settings.md_skill_zip_max_total_mb * 1024 * 1024
    total = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = [info for info in zf.infolist() if not info.filename.endswith("/")]
            if len(infos) > settings.md_skill_zip_max_entries:
                raise ApiError("AI_SKILL_TOO_LARGE", "zip contains too many entries", 422)
            for info in infos:
                parts = Path(info.filename).parts
                if info.filename.startswith("/") or "\\" in info.filename or ".." in parts:
                    raise ApiError("AI_SKILL_PATH_INVALID", "zip entry path is invalid", 422)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ApiError("AI_SKILL_PATH_INVALID", "zip entry is a symbolic link", 422)
                if Path(info.filename).suffix.lower() not in _ALLOWED:
                    raise ApiError("AI_SKILL_FILE_TYPE_UNSUPPORTED", "skill file type is not allowed", 422)
                dest = (stage_root / info.filename).resolve()
                if stage_root.resolve() not in dest.parents:
                    raise ApiError("AI_SKILL_PATH_INVALID", "zip entry path escapes staging root", 422)
                dest.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with zf.open(info) as src, dest.open("wb") as out:
                    while chunk := src.read(1024 * 1024):
                        written += len(chunk)
                        if total + written > max_total_bytes:
                            raise ApiError("AI_SKILL_TOO_LARGE", "zip content exceeds configured limit", 422)
                        out.write(chunk)
                total += written
    except zipfile.BadZipFile as exc:
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "zip archive is corrupted", 415) from exc


def _locate_skill_dir(stage_root: Path) -> str:
    """定位 zip 内技能目录：单顶层目录，或根级 SKILL.md（按 front matter name 归入同名目录）。"""
    entries = list(stage_root.iterdir())
    dirs = [p for p in entries if p.is_dir()]
    loose = [p for p in entries if not p.is_dir()]
    if len(dirs) == 1 and not loose:
        return dirs[0].name
    if not dirs:
        md = next((p for p in loose if p.name.lower() == "skill.md"), None)
        if md is not None:
            try:
                text = md.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ApiError("AI_SKILL_INVALID_MANIFEST", "SKILL.md must be UTF-8 encoded", 422) from exc
            name = peek_skill_name(text)
            skill_dir = stage_root / name
            skill_dir.mkdir()
            for item in loose:
                target_name = "SKILL.md" if item.name.lower() == "skill.md" else item.name
                shutil.move(str(item), str(skill_dir / target_name))
            return name
    raise ApiError("AI_SKILL_INVALID_MANIFEST", "zip must contain a single skill directory or a root SKILL.md", 422)


def _stage_single_md(md_path: Path, stage_root: Path) -> str:
    """单个 .md 文件：读取内容并按 front matter name 落为 {name}/SKILL.md。"""
    try:
        text = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "file is not valid UTF-8 text", 415) from exc
    name = peek_skill_name(text)
    skill_dir = stage_root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return name


def ingest_upload(db: Session, *, tenant_id: UUID, owner_user_id: UUID | None, filename: str, tmp_path: Path, trace_id: str | None = None) -> dict[str, Any]:
    """解析上传的 zip/.md 技能包：staging 校验 → 落盘替换 md_skills_dir/{name} → 注册 Skill + 草稿版本。

    校验失败不在共享目录留下任何文件；落盘后注册/提交失败则回滚文件替换。
    """
    settings = get_settings()
    suffix = Path(filename).suffix.lower()
    if suffix not in {".zip", ".md"}:
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "file type is not supported", 415)
    with tempfile.TemporaryDirectory(prefix="md-skill-upload-") as tmp:
        tmp_root = Path(tmp)
        stage_root = tmp_root / "stage"
        stage_root.mkdir()
        if suffix == ".zip":
            if tmp_path.read_bytes()[:4] != _ZIP_MAGIC:
                raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "file content does not match its type", 415)
            _extract_zip(tmp_path, stage_root)
            skill_dir_name = _locate_skill_dir(stage_root)
        else:
            skill_dir_name = _stage_single_md(tmp_path, stage_root)
        try:
            manifest = parse_skill(stage_root, skill_dir_name)
        except UnicodeDecodeError as exc:
            raise ApiError("AI_SKILL_INVALID_MANIFEST", "SKILL.md must be UTF-8 encoded", 422) from exc
        conflict = db.scalar(select(Skill).where(Skill.tenant_id == tenant_id, Skill.name == manifest.name, Skill.status == "ACTIVE", Skill.skill_type != "MD_SKILL"))
        if conflict is not None:
            raise ApiError("AI_SKILL_NAME_CONFLICT", "a non MD-Skill skill with the same name already exists", 409)
        target_root = Path(settings.md_skills_dir)
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / manifest.name
        backup = tmp_root / "backup" / manifest.name
        replaced = target.exists()
        if replaced:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        placed = False
        try:
            shutil.move(str(stage_root / manifest.name), str(target))
            placed = True
            skill, version = register_skill_dir(db, tenant_id=tenant_id, owner_user_id=owner_user_id, root=target_root, dir_name=manifest.name)
            status = "draft_created" if version is not None else "unchanged"
            record_audit(db, tenant_id=tenant_id, actor_user_id=owner_user_id, action="MD_SKILL_UPLOADED", resource_type="SKILL", resource_id=skill.id, summary="MD-Skill uploaded", detail={"filename": filename, "fingerprint": manifest.file_fingerprint, "status": status}, trace_id=trace_id)
            db.commit()
        except Exception:
            if placed:
                shutil.rmtree(target, ignore_errors=True)
            if replaced:
                shutil.move(str(backup), str(target))
            raise
        return {"skill_id": str(skill.id), "name": skill.name, "version_number": version.version_number if version is not None else None, "status": status, "fingerprint": manifest.file_fingerprint}
