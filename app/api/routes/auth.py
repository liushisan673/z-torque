from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_session
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.models import AuthSession, Permission, Role, User, role_permission, user_role
from app.schemas.auth import LoginRequest, RefreshRequest
from app.security.tokens import create_access_token, create_refresh_token, hash_refresh_token
from app.services.auth_service import authenticate

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", summary="登录")
async def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    access, refresh, _ = authenticate(db, payload.username, payload.password)
    response.set_cookie("access_token", access, httponly=True, samesite="lax")
    return success_response(request, {"access_token": access, "refresh_token": refresh, "token_type": "bearer"})


@router.post("/refresh", summary="刷新令牌")
async def refresh(payload: RefreshRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    session = get_session(db, payload.refresh_token)
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise ApiError("AI_AUTH_FAILED", "authentication failed", 401)
    new_refresh = create_refresh_token()
    session.refresh_token_hash = hash_refresh_token(new_refresh)
    session.last_used_at = datetime.now(timezone.utc)
    db.commit()
    access = create_access_token(str(user.id), str(session.id), str(user.tenant_id))
    response.set_cookie("access_token", access, httponly=True, samesite="lax")
    return success_response(request, {"access_token": access, "refresh_token": new_refresh, "token_type": "bearer"})


@router.post("/logout", summary="登出")
async def logout(request: Request, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    authorization = request.headers.get("Authorization", "")
    if not token and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token:
        from app.security.tokens import decode_access_token
        claims = decode_access_token(token)
        session = db.get(AuthSession, claims["sid"])
        if session:
            session.revoked_at = datetime.now(timezone.utc)
            db.commit()
    response.delete_cookie("access_token")
    return success_response(request, {"logged_out": True})


@router.get("/me", summary="获取当前用户信息")
async def me(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    role_names = [
        r[0]
        for r in db.execute(
            select(Role.name)
            .select_from(user_role)
            .join(Role, Role.id == user_role.c.role_id)
            .where(user_role.c.user_id == user.id)
        ).all()
    ]
    perm_names = [
        p[0]
        for p in db.execute(
            select(Permission.name)
            .select_from(user_role)
            .join(Role, Role.id == user_role.c.role_id)
            .join(role_permission, role_permission.c.role_id == Role.id)
            .join(Permission, Permission.id == role_permission.c.permission_id)
            .where(user_role.c.user_id == user.id)
            .distinct()
        ).all()
    ]
    role_names.sort()
    perm_names.sort()
    return success_response(
        request,
        {
            "id": str(user.id),
            "username": user.username,
            "tenant_id": str(user.tenant_id),
            "roles": role_names,
            "permissions": perm_names,
        },
    )
