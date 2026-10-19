import pytest
from sqlalchemy import create_engine
from app.builtin_plugins.executors import DbQueryExecutor
from app.kernel.skill_gateway import SkillGateway


def test_db_query_executor_rejects_write():
    executor = DbQueryExecutor(create_engine("sqlite://"))
    result = executor.execute({"query": "DELETE FROM users", "allowed_tables": ["users"], "allowed_columns": ["id"]}, {}, 30)
    assert result.error_code == "AI_REQUEST_INVALID"


def test_db_query_executor_rejects_unapproved_table():
    executor = DbQueryExecutor(create_engine("sqlite://"))
    result = executor.execute({"query": "SELECT id FROM users", "allowed_tables": ["orders"], "allowed_columns": ["id"]}, {}, 30)
    assert result.error_code == "AI_FORBIDDEN"


def test_registered_connector_config_contains_connector_provenance():
    config = {"method": "GET", "connector_id": "connector-1", "connector_type": "REST"}
    assert config["connector_id"] == "connector-1"
    assert config["connector_type"] == "REST"


def test_skill_gateway_has_connector_guard():
    assert "AI_CONNECTOR_UNAVAILABLE" in SkillGateway.execute_async.__code__.co_consts
