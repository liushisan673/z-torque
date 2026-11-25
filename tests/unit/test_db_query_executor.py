from sqlalchemy import create_engine

from app.builtin_plugins.executors import DbQueryExecutor


def _executor():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE allowed_records (id INTEGER PRIMARY KEY, name TEXT, secret TEXT)")
        connection.exec_driver_sql(
            "INSERT INTO allowed_records (name, secret) VALUES "
            "('one', 's1'), ('two', 's2'), ('three', 's3')"
        )
    return engine, DbQueryExecutor(engine)


def test_db_query_applies_sql_row_limit():
    engine, executor = _executor()
    try:
        result = executor.execute(
            {
                "query": "SELECT id FROM allowed_records ORDER BY id",
                "allowed_tables": ["allowed_records"],
                "allowed_columns": ["id"],
                "max_rows": 2,
            },
            {},
            1.0,
        )
        assert result.status == "SUCCEEDED"
        assert len(result.data) == 2
    finally:
        engine.dispose()


def test_db_query_rejects_select_star_column_allowlist_bypass():
    engine, executor = _executor()
    try:
        result = executor.execute(
            {
                "query": "SELECT * FROM allowed_records",
                "allowed_tables": ["allowed_records"],
                "allowed_columns": ["id"],
                "max_rows": 10,
            },
            {},
            1.0,
        )
        assert result.status == "FAILED"
        assert result.error_code == "AI_FORBIDDEN"
    finally:
        engine.dispose()


def test_db_query_rejects_dml_inside_cte():
    engine, executor = _executor()
    try:
        result = executor.execute(
            {
                "query": "WITH removed AS (DELETE FROM allowed_records RETURNING id) SELECT id FROM removed",
                "allowed_tables": ["allowed_records"],
                "allowed_columns": ["id"],
                "max_rows": 10,
            },
            {},
            1.0,
        )
        assert result.status == "FAILED"
    finally:
        engine.dispose()
