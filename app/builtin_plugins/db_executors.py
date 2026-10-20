"""连接器目标数据库执行器插件实现（tool_type=DB_SQL，能力实现，G2a 迁入）。

直连"连接器配置的目标库"的运行时执行器；注册期/执行期共用的 SQL 校验策略
（render_sql / validate_sql_statement）与探测策略（probe_database）属契约层，
保留在 app/kernel/db_executor.py，本模块按插件→内核方向引用（阶段00 §3.8 允许）。

行为契约（与迁移前一致，见内核 db_executor 模块文档）：
- SQL 模板中 {{参数}} 一律转换为数据库【绑定变量】（:name），参数值永不拼接进 SQL，防注入；
- 写入仅允许单条 INSERT/UPDATE/DELETE（UPDATE/DELETE 必须带 WHERE）；禁止 DDL / 管理命令；
- 所有语句引用的表必须落在 allowed_tables 白名单内（与连接器当前白名单取交集，fail-closed）；
- 写入语句返回受影响行数，执行后提交，由注册路由强制 requires_confirmation 走人工审批。
"""

from typing import Any

from app.kernel.db_executor import (
    _DEFAULT_MAX_ROWS,
    _MAX_ROWS_CAP,
    _conn_params,
    _json_safe,
    _named_to_mysql,
    _open_connection,
    _short_error,
    validate_sql_statement,
)
from app.kernel.ports.skill_executor import ExecutionResult


class ConnectorDbExecutor:
    """连接器 DB 工具执行器（tool_type=DB_SQL）：绑定变量直连目标库，支持只读查询与受审批的写入。"""

    tool_type = "DB_SQL"

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: float) -> ExecutionResult:
        template = str(config.get("sql_template") or "").strip()
        if not template:
            return ExecutionResult(status="FAILED", error_code="AI_DB_CONFIG_INVALID", error_message="SQL template is missing")
        allowed = {str(table) for table in (config.get("allowed_tables") or [])}
        connector_allowed = config.get("connector_allowed_tables")
        if isinstance(connector_allowed, list):
            # 连接器当前白名单与工具注册快照取交集，双向收紧（fail-closed）
            allowed &= {str(table) for table in connector_allowed}
        if not allowed:
            return ExecutionResult(status="FAILED", error_code="AI_DB_FORBIDDEN", error_message="no allowed tables configured")
        driver = str(config.get("driver") or "postgresql").lower()
        try:
            sql, params, is_write = validate_sql_statement(template, allowed, driver=driver, allow_write=True)
            missing = [name for name in params if name not in arguments]
            if missing:
                return ExecutionResult(status="FAILED", error_code="AI_PARAM_INVALID", error_message=f"缺少绑定参数：{', '.join(missing)}")
            bind = {name: arguments[name] for name in params}
            if driver == "mysql":
                sql = _named_to_mysql(sql)
        except ValueError as exc:
            return ExecutionResult(status="FAILED", error_code="AI_DB_QUERY_INVALID", error_message=str(exc))
        max_rows = _DEFAULT_MAX_ROWS
        if not is_write:
            try:
                max_rows = int(config.get("max_rows") or _DEFAULT_MAX_ROWS)
            except (TypeError, ValueError):
                max_rows = _DEFAULT_MAX_ROWS
            max_rows = min(max(max_rows, 1), _MAX_ROWS_CAP)
        try:
            resolved_driver, conn_params = _conn_params(config, config.get("secret_value"), timeout_seconds)
        except ValueError as exc:
            return ExecutionResult(status="FAILED", error_code="AI_DB_CONFIG_INVALID", error_message=str(exc))
        try:
            if is_write:
                affected = self._run_write(resolved_driver, conn_params, sql, bind)
                return ExecutionResult(status="SUCCEEDED", data={"write": True, "rows_affected": affected})
            rows = self._run_read(resolved_driver, conn_params, sql, bind, max_rows + 1)
        except ValueError as exc:
            return ExecutionResult(status="FAILED", error_code="AI_DB_CONFIG_INVALID", error_message=str(exc))
        except Exception as exc:
            return ExecutionResult(status="FAILED", error_code="AI_CONNECTOR_UNAVAILABLE", error_message=f"数据库执行失败：{_short_error(exc)}")
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return ExecutionResult(
            status="SUCCEEDED",
            data={
                "rows": [_json_safe(row) for row in rows],
                "row_count": len(rows),
                "truncated": truncated,
                "columns": list(rows[0].keys()) if rows else [],
            },
        )

    @staticmethod
    def _run_read(driver: str, params: dict[str, Any], sql: str, bind: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """建立连接执行查询，返回原始行（dict）。"""
        connection = _open_connection(driver, params)
        try:
            cursor = connection.cursor()
            cursor.execute(sql, bind)
            columns = [item[0] for item in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(limit)
            return [dict(zip(columns, row)) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _run_write(driver: str, params: dict[str, Any], sql: str, bind: dict[str, Any]) -> int:
        """执行写入语句：提交事务并返回受影响行数；失败回滚。"""
        connection = _open_connection(driver, params)
        try:
            cursor = connection.cursor()
            cursor.execute(sql, bind)
            affected = cursor.rowcount
            connection.commit()
            return int(affected) if affected is not None and int(affected) > 0 else 0
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            connection.close()
