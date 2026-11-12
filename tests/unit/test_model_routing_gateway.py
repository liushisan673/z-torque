"""模型路由网关：按 ModelResource → Provider → Endpoint 选择实际模型网关。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.builtin_plugins.model_gateway import ModelRoutingGateway
from app.db.session import SessionLocal
from app.kernel.model_gateway.contracts import ModelMessage, ModelRequest
from app.models import ModelEndpoint, ModelProvider, ModelResource, Tenant, User
from app.security.passwords import hash_password
from app.security.secret_store import SecretStore


class _DefaultGateway:
    default_model = "fallback-model"


@pytest.fixture()
def routing_env():
    with SessionLocal() as db:
        tenant = Tenant(name=f"routing-{uuid4()}")
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username=f"routing-{uuid4()}", password_hash=hash_password("pass"))
        db.add(owner)
        db.flush()
        provider = ModelProvider(
            tenant_id=tenant.id,
            created_by=owner.id,
            name="qwen-provider",
            provider_type="openai_compatible",
            status="ENABLED",
            base_url="https://provider.example.com/v1",
            secret_ref="secret://" + SecretStore().encrypt("provider-key"),
            config={},
        )
        db.add(provider)
        db.flush()
        model = ModelResource(
            tenant_id=tenant.id,
            provider_id=provider.id,
            name="qwen",
            model_identifier="qwen-test",
            status="ENABLED",
            config={"supports_vision": True},
        )
        db.add(model)
        db.flush()
        db.add(
            ModelEndpoint(
                tenant_id=tenant.id,
                provider_id=provider.id,
                model_id=model.id,
                name="default",
                endpoint_url="https://endpoint.example.com/v1",
                status="ENABLED",
                priority=0,
                config={},
            )
        )
        db.commit()
        ids = {"tenant": tenant.id}
    yield ids
    with SessionLocal.begin() as db:
        for table in (ModelEndpoint, ModelResource, ModelProvider, User):
            db.execute(delete(table).where(table.tenant_id == ids["tenant"]))
        db.execute(delete(Tenant).where(Tenant.id == ids["tenant"]))


def _request(tenant_id, model="qwen-test") -> ModelRequest:
    return ModelRequest(trace_id="t", messages=[ModelMessage("user", "hi")], model=model, tenant_id=tenant_id)


def test_routes_to_model_management_endpoint(routing_env):
    gateway = ModelRoutingGateway(session_factory=SessionLocal, default_gateway=_DefaultGateway())
    resolved = gateway._resolve(_request(routing_env["tenant"]))
    assert resolved is not None
    assert resolved.base_url == "https://endpoint.example.com/v1"
    assert resolved.default_model == "qwen-test"


def test_unknown_model_falls_back_to_global_gateway(routing_env):
    default = _DefaultGateway()
    gateway = ModelRoutingGateway(session_factory=SessionLocal, default_gateway=default)
    routed, model = gateway._route(_request(routing_env["tenant"], model="unmanaged-model"))
    assert routed is default
    assert model == "fallback-model"
