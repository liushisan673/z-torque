from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass
class ApiError(Exception):
    code: str
    message: str
    status_code: int = 400
    data: Any = None


def trace_id_from(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def error_response(request: Request, error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "code": error.code,
            "message": error.message,
            "data": error.data,
            "trace_id": trace_id_from(request),
        },
    )


def success_response(request: Request, data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data, "trace_id": trace_id_from(request)}
