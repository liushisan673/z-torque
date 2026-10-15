from dataclasses import dataclass
from typing import Any


MD_SKILL_ERROR_CODES = {
    "AI_SKILL_INVALID_MANIFEST",
    "AI_SKILL_INVALID_SCHEMA",
    "AI_SKILL_FILE_TYPE_UNSUPPORTED",
    "AI_SKILL_PATH_INVALID",
    "AI_SKILL_METADATA_INVALID",
    "AI_SKILL_CONTENT_INVALID",
    "AI_CAPABILITY_UNAVAILABLE",
    "AI_RESOURCE_FORBIDDEN",
    "AI_PARAM_INVALID",
    "AI_TASK_RESULT_INVALID",
}

TASK_ERROR_CODES = {
    "AI_TASK_INVALID_TRANSITION",
    "AI_TASK_ALREADY_TERMINAL",
    "AI_TASK_NOT_CANCELABLE",
    "AI_TASK_DUPLICATE_IDEMPOTENCY",
}


@dataclass
class ApiError(Exception):
    code: str
    message: str
    status_code: int = 400
    data: Any = None
