from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.kernel.md_skill import MdSkillManifest, parse_skill, snapshot
from app.models import Skill, SkillVersion


@dataclass(frozen=True)
class ScanIssue:
    path: str
    message: str
    code: str


@dataclass(frozen=True)
class ScanResult:
    discovered: int
    drafts_created: int
    unchanged: int
    issues: tuple[ScanIssue, ...]


def _manifest_version(db: Session, skill: Skill, manifest: MdSkillManifest) -> SkillVersion | None:
    versions = db.scalars(select(SkillVersion).where(SkillVersion.skill_id == skill.id).order_by(SkillVersion.version_number.desc())).all()
    for version in versions:
        if version.config_snapshot.get("file_fingerprint") == manifest.file_fingerprint:
            return version
    return None


def _create_draft(db: Session, *, tenant_id: UUID, owner_user_id: UUID | None, skill: Skill, manifest: MdSkillManifest) -> SkillVersion:
    number = (db.scalar(select(func.max(SkillVersion.version_number)).where(SkillVersion.skill_id == skill.id)) or 0) + 1
    item = SkillVersion(
        tenant_id=tenant_id,
        skill_id=skill.id,
        version_number=number,
        status="DRAFT",
        config_snapshot=snapshot(manifest),
        instructions=manifest.instructions,
        script_source=None,
        required_permissions=manifest.required_permissions,
        requires_confirmation=False,
    )
    db.add(item)
    db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=owner_user_id, action="MD_SKILL_DRAFT_CREATED", resource_type="SKILL_VERSION", resource_id=item.id, summary="MD-Skill draft discovered", detail={"path": manifest.relative_path, "fingerprint": manifest.file_fingerprint})
    return item


def register_skill_dir(db: Session, *, tenant_id: UUID, owner_user_id: UUID | None, root: str | Path, dir_name: str) -> tuple[Skill, SkillVersion | None]:
    """解析 root 下单个技能目录并注册：查/建 MD_SKILL，指纹为新时创建草稿版本。

    返回 (skill, 新建的草稿版本或 None)；None 表示指纹与既有版本一致（unchanged）。
    解析失败抛 ApiError，由调用方决定如何处理。
    """
    manifest = parse_skill(root, dir_name)
    skill = db.scalar(select(Skill).where(Skill.tenant_id == tenant_id, Skill.name == manifest.name, Skill.status == "ACTIVE"))
    if skill is None:
        skill = Skill(tenant_id=tenant_id, owner_user_id=owner_user_id, name=manifest.name, description=manifest.description, skill_type="MD_SKILL", source_type="USER", status="ACTIVE", official=False)
        db.add(skill)
        db.flush()
        record_audit(db, tenant_id=tenant_id, actor_user_id=owner_user_id, action="MD_SKILL_REGISTERED", resource_type="SKILL", resource_id=skill.id, summary="MD-Skill registered")
    version = None
    if _manifest_version(db, skill, manifest) is None:
        version = _create_draft(db, tenant_id=tenant_id, owner_user_id=owner_user_id, skill=skill, manifest=manifest)
    return skill, version


def scan_directory(db: Session, *, tenant_id: UUID, root: str | Path, owner_user_id: UUID | None = None) -> ScanResult:
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    discovered = drafts = unchanged = 0
    issues: list[ScanIssue] = []
    for directory in sorted(path for path in root_path.iterdir() if path.is_dir() and not path.is_symlink()):
        discovered += 1
        try:
            _, version = register_skill_dir(db, tenant_id=tenant_id, owner_user_id=owner_user_id, root=root_path, dir_name=directory.name)
            if version is None:
                unchanged += 1
            else:
                drafts += 1
        except ApiError as exc:
            issues.append(ScanIssue(directory.name, exc.message, exc.code))
    db.flush()
    return ScanResult(discovered, drafts, unchanged, tuple(issues))
