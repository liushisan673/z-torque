from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.task.service import create_task
from app.models import Agent, AgentVersion, Conversation, Message, Tenant, User
from app.security.passwords import hash_password
from app.services.chat_service import handle_mock_chat


def _fixture(db):
    tenant = Tenant(name=f"stage3-{uuid4()}")
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
    db.add(user)
    db.flush()
    agent = Agent(tenant_id=tenant.id, owner_user_id=user.id, name="agent", source_type="USER", status="ACTIVE")
    db.add(agent)
    db.flush()
    version = AgentVersion(tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={"system_prompt": "test"})
    db.add(version)
    db.flush()
    agent.current_published_version_id = version.id
    conversation = Conversation(tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, status="ACTIVE")
    db.add(conversation)
    db.flush()
    return tenant, user, agent, version, conversation


def test_mock_chat_does_not_create_task():
    with SessionLocal() as db:
        tenant, user, _, _, conversation = _fixture(db)
        result = handle_mock_chat(db, tenant_id=tenant.id, user_id=user.id, conversation_id=conversation.id, content="hello")
        assert result.task_id is None
        assert result.assistant_message is not None
        assert db.scalar(select(Message).where(Message.id == result.user_message.id)).task_id is None
        db.rollback()


def test_trigger_alias_mapping_and_conflict():
    with SessionLocal() as db:
        tenant, user, agent, version, conversation = _fixture(db)
        message = Message(tenant_id=tenant.id, conversation_id=conversation.id, sender_type="USER", content="run", status="COMPLETED")
        db.add(message)
        db.flush()
        task = create_task(db, tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, agent_version_id=version.id, idempotency_key=str(uuid4()), request_summary="run", conversation_id=conversation.id, message_id=message.id)
        assert task.trigger_message_id == message.id
        assert task.message_id == message.id
        assert message.task_id == task.id
        with pytest.raises(ApiError) as error:
            create_task(db, tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, agent_version_id=version.id, idempotency_key=str(uuid4()), request_summary="run", conversation_id=conversation.id, trigger_message_id=message.id, message_id=uuid4())
        assert error.value.code == "AI_REQUEST_INVALID"
        db.rollback()