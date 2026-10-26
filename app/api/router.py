from fastapi import APIRouter

from app.api.routes.a2a import router as a2a_router
from app.api.routes.admin_iam import router as admin_iam_router
from app.api.routes.agents import router as agents_router
from app.api.routes.audit import router as audit_router
from app.api.routes.audit_exports import archive_router as audit_archive_router
from app.api.routes.audit_exports import router as audit_exports_router
from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.confirmations import router as confirmations_router
from app.api.routes.connectors import router as connectors_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.files import router as files_router
from app.api.routes.md_skills import router as md_skills_router
from app.api.routes.messages import router as messages_router
from app.api.routes.models import router as models_router
from app.api.routes.secrets import router as secrets_router
from app.api.routes.skills_tools import router as skills_tools_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.users_roles_compat import router as users_roles_compat_router

router = APIRouter(prefix="/api/z-torque")
router.include_router(auth_router)
router.include_router(admin_iam_router)
router.include_router(users_roles_compat_router)
router.include_router(tasks_router)
router.include_router(agents_router)
router.include_router(conversations_router)
router.include_router(messages_router)
router.include_router(skills_tools_router)
router.include_router(confirmations_router)
router.include_router(chat_router)
router.include_router(md_skills_router)
router.include_router(connectors_router)
router.include_router(models_router)
router.include_router(files_router)
router.include_router(audit_router)
router.include_router(audit_exports_router)
router.include_router(audit_archive_router)
router.include_router(a2a_router)
router.include_router(secrets_router)
