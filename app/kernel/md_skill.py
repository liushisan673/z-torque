from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, SchemaError, validate

from app.core.errors import ApiError

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_ALLOWED = {".md", ".py", ".json", ".csv", ".txt"}


def normalize_newlines(text: str) -> str:
    """统一换行为 LF：Windows 上传的 SKILL.md（CRLF）同样能识别 YAML front matter。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class MdSkillManifest:
    name: str
    description: str
    implementation: str
    declared_version: int
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allow_scripts: bool
    required_permissions: list[str]
    instructions: str
    script_files: list[str]
    file_fingerprint: str
    relative_path: str


def _safe_path(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if root not in resolved.parents:
        raise ApiError("AI_SKILL_PATH_INVALID", "skill path escapes configured root", 422)
    return resolved


class MdSkillInstructionLoader:
    """Load declarative SKILL.md content without executing embedded scripts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def load(self, relative_path: str) -> dict[str, Any]:
        path = (self.root / relative_path).resolve()
        if path.parent != self.root and self.root not in path.parents:
            raise ApiError("AI_SKILL_PATH_INVALID", "skill path escapes configured root", 422)
        if path.name != "SKILL.md" or not path.is_file():
            raise ApiError("AI_SKILL_NOT_FOUND", "SKILL.md not found", 404)
        text = normalize_newlines(path.read_text(encoding="utf-8"))
        if not text.startswith("---\n"):
            raise ApiError("AI_SKILL_METADATA_INVALID", "SKILL.md requires YAML front matter", 422)
        marker = text.find("\n---\n", 4)
        if marker < 0:
            raise ApiError("AI_SKILL_METADATA_INVALID", "SKILL.md front matter is not closed", 422)
        try:
            metadata = yaml.safe_load(text[4:marker]) or {}
        except yaml.YAMLError as exc:
            raise ApiError("AI_SKILL_METADATA_INVALID", "SKILL.md front matter is invalid", 422) from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not isinstance(metadata.get("description"), str):
            raise ApiError("AI_SKILL_METADATA_INVALID", "SKILL.md requires name and description", 422)
        instructions = text[marker + 5:].strip()
        if not instructions:
            raise ApiError("AI_SKILL_CONTENT_INVALID", "SKILL.md instructions are empty", 422)
        return {"metadata": metadata, "instructions": instructions, "path": str(path)}


def parse_skill(root: str | Path, skill_dir: str | Path) -> MdSkillManifest:
    root_path = Path(root).resolve()
    directory = _safe_path(root_path, root_path / skill_dir)
    if not directory.is_dir() or directory.name != str(directory.name):
        raise ApiError("AI_SKILL_NOT_FOUND", "skill directory not found", 404)
    if not _NAME.fullmatch(directory.name):
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "skill directory name is invalid", 422)
    manifest_path = _safe_path(root_path, directory / "SKILL.md")
    if not manifest_path.is_file():
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "SKILL.md is required", 422)
    text = normalize_newlines(manifest_path.read_text(encoding="utf-8"))
    if not text.startswith("---\n"):
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "YAML front matter is required", 422)
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "YAML front matter is not closed", 422)
    try:
        meta = yaml.safe_load(text[4:marker]) or {}
    except yaml.YAMLError as exc:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "YAML front matter is invalid", 422) from exc
    if not isinstance(meta, dict):
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "front matter must be an object", 422)
    required = ("name", "description", "implementation", "version", "input_schema", "output_schema", "allow_scripts", "required_permissions")
    if any(key not in meta for key in required):
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "required manifest field is missing", 422)
    if not isinstance(meta["name"], str) or meta["name"] != directory.name or not _NAME.fullmatch(meta["name"]):
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "skill name is invalid", 422)
    if not isinstance(meta["description"], str) or not meta["description"].strip():
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "skill description is invalid", 422)
    implementation = meta["implementation"]
    if implementation not in {"md_instruction", "md_script"} or not isinstance(meta["version"], int) or meta["version"] < 1:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "implementation or version is invalid", 422)
    if not isinstance(meta["allow_scripts"], bool) or not isinstance(meta["required_permissions"], list) or not all(isinstance(x, str) for x in meta["required_permissions"]):
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "script or permission fields are invalid", 422)
    if implementation == "md_script" and not meta["allow_scripts"]:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "script implementation requires allow_scripts", 422)
    for schema_key in ("input_schema", "output_schema"):
        if not isinstance(meta[schema_key], dict):
            raise ApiError("AI_SKILL_INVALID_SCHEMA", f"{schema_key} must be an object", 422)
        try:
            Draft202012Validator.check_schema(meta[schema_key])
        except SchemaError as exc:
            raise ApiError("AI_SKILL_INVALID_SCHEMA", f"{schema_key} is invalid", 422) from exc
    instructions = text[marker + 5:].strip()
    if not instructions:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "skill instructions are empty", 422)
    files: list[tuple[str, bytes]] = []
    for item in sorted(directory.rglob("*")):
        safe = _safe_path(root_path, item)
        if safe.is_symlink() or safe.is_dir():
            continue
        relative = safe.relative_to(directory).as_posix()
        if relative == "README.md":
            continue
        if safe.suffix.lower() not in _ALLOWED:
            raise ApiError("AI_SKILL_FILE_TYPE_UNSUPPORTED", "skill file type is not allowed", 422)
        files.append((relative, safe.read_bytes()))
    scripts = [name for name, _ in files if name.startswith("scripts/") and name.endswith(".py")]
    if implementation == "md_script" and not scripts:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "script skill requires a script file", 422)
    if implementation == "md_instruction" and scripts:
        scripts = []
    digest = hashlib.sha256()
    for name, content in files:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(content)
    return MdSkillManifest(meta["name"], meta["description"], implementation, meta["version"], meta["input_schema"], meta["output_schema"], meta["allow_scripts"], meta["required_permissions"], instructions, scripts, digest.hexdigest(), directory.relative_to(root_path).as_posix())


def validate_input(manifest: MdSkillManifest, value: dict[str, Any]) -> None:
    try:
        validate(value, manifest.input_schema)
    except Exception as exc:
        raise ApiError("AI_PARAM_INVALID", "skill input does not match schema", 400) from exc


def snapshot(manifest: MdSkillManifest) -> dict[str, Any]:
    return {"name": manifest.name, "description": manifest.description, "implementation": manifest.implementation, "declared_version": manifest.declared_version, "input_schema": manifest.input_schema, "output_schema": manifest.output_schema, "allow_scripts": manifest.allow_scripts, "required_permissions": manifest.required_permissions, "instructions": manifest.instructions, "script_files": manifest.script_files, "file_fingerprint": manifest.file_fingerprint, "relative_path": manifest.relative_path}
