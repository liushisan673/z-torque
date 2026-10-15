"""连接器目标数据库执行器（只读查询 + 需人工审批的写入语句）。

与 builtin_executors.DbQueryExecutor（应用自身 Engine）不同，本执行器直连"连接器配置的目标库"：
- SQL 模板中 {{参数}} 一律转换为数据库【绑定变量】（:name），参数值永不拼接进 SQL，防注入；
- 只读查询支持 SELECT（含 JOIN / UNION / 子查询 / WITH CTE）；写入仅允许单条 INSERT/UPDATE/DELETE
  （UPDATE/DELETE 必须带 WHERE）；任何语句都禁止 DDL / 管理命令节点（黑名单校验，注册期与执行期共用）；
- 所有语句引用的表必须落在 allowed_tables 白名单内（CTE 别名除外），行数上限默认 100（上限 1000）；
- 写入语句返回受影响行数，执行后提交，由注册路由强制 requires_confirmation 走人工审批；
- 支持 postgresql / mysql / sqlite 驱动；连接信息取自连接器 config，密码取自解密后的 secret_value。
"""

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import pg8000.dbapi
from sqlglot import exp, parse

# 本进程内 pg8000 仅由本模块使用：统一 named 参数风格（:name 绑定变量）
pg8000.dbapi.paramstyle = "named"

from app.kernel.skill_gateway import ExecutionResult

# 模板占位符：{{参数名}}（与 HTTP executor 外观一致，但语义是绑定变量而非字符串替换）
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
_PARAM_NAME = re.compile(r"[A-Za-z0-9_]+")
_MYSQL_NAMED = re.compile(r":([A-Za-z0-9_]+)")
_DEFAULT_MAX_ROWS = 100
_MAX_ROWS_CAP = 1000
_PROBE_TIMEOUT_SECONDS = 10.0

# 驱动 → sqlglot 方言
_DIALECTS = {"postgresql": "postgres", "mysql": "mysql", "sqlite": "sqlite"}
# 写入类语句根节点
_WRITE_ROOTS = (exp.Insert, exp.Update, exp.Delete)
# 只读语句根节点（UNION/EXCEPT/INTERSECT 均为 SetOperation 子类；WITH 前缀解析在 Select 上）
_READ_ROOTS = (exp.Select, exp.SetOperation, exp.Subquery)
# 任何语句（含写入工具）的任何位置都禁止出现的节点：DDL、会话/管理命令、任意命令兜底
_HARD_FORBIDDEN = (
    exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
    exp.Copy, exp.Grant, exp.Set, exp.Use, exp.Analyze, exp.Command,
)
# 只读工具额外禁止：DML、SELECT INTO、FOR UPDATE 行锁
_READ_EXTRA_FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Into, exp.Lock)


def render_sql(sql_template: str) -> tuple[str, list[str]]:
    """把 SQL 模板中的 {{参数}} 转换为具名绑定变量 :name。

    返回 (可执行 SQL, 参数名列表)。参数名只允许字母数字下划线；
    模板中出现无法识别的占位符时抛 ValueError。
    """
    params: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "secret" or not _PARAM_NAME.fullmatch(name):
            raise ValueError(f"绑定参数名 {name} 不合法（仅允许字母、数字、下划线，且不允许 {{secret}}）")
        if name not in params:
            params.append(name)
        return f":{name}"

    sql = _TEMPLATE.sub(substitute, sql_template)
    if "{{" in sql or "}}" in sql:
        raise ValueError("SQL 模板存在无法解析的占位符")
    return sql, params


def validate_sql_statement(
    sql_template: str,
    allowed_tables: set[str],
    *,
    driver: str = "postgresql",
    allow_write: bool = False,
) -> tuple[str, list[str], bool]:
    """解析并校验单条 SQL 语句；注册期与执行期共用。

    规则：
    - 单条语句；只读工具仅允许 SELECT（含 JOIN/UNION/子查询/WITH），allow_write 时额外允许
      单条 INSERT/UPDATE/DELETE（UPDATE/DELETE 必须带 WHERE）；
    - 语句树中禁止任何 DDL / 管理命令节点（黑名单法），只读工具额外禁止 DML 与 SELECT INTO / FOR UPDATE；
    - 引用的实体表必须在 allowed_tables 白名单内（CTE 别名不算实体表）；
    - 返回 (渲染后 SQL, 绑定参数名列表, 是否写入语句)。
    """
    dialect = _DIALECTS.get(driver)
    if dialect is None:
        raise ValueError(f"暂不支持的数据库驱动：{driver}")
    sql, params = render_sql(sql_template)
    try:
        statements = parse(sql, read=dialect)
    except Exception as exc:
        raise ValueError(f"SQL 解析失败：{exc}") from exc
    if len(statements) != 1:
        raise ValueError("仅允许单条 SQL 语句")
    statement = statements[0]
    is_write = isinstance(statement, _WRITE_ROOTS)
    if is_write and not allow_write:
        raise ValueError("该工具为只读工具，仅允许 SELECT 查询（支持 JOIN / UNION / 子查询 / WITH）")
    if not is_write and not isinstance(statement, _READ_ROOTS):
        raise ValueError("仅允许单条 SELECT 查询（支持 JOIN / UNION / 子查询 / WITH）或单条写入语句（INSERT/UPDATE/DELETE）")
    forbidden = _HARD_FORBIDDEN if is_write else _HARD_FORBIDDEN + _READ_EXTRA_FORBIDDEN
    for node in statement.walk():
        if isinstance(node, forbidden):
            raise ValueError("SQL 包含不允许的 DDL / 管理命令节点")
    if is_write and isinstance(statement, (exp.Update, exp.Delete)) and statement.args.get("where") is None:
        raise ValueError("UPDATE / DELETE 语句必须包含 WHERE 条件")
    cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    tables = {table.name for table in statement.find_all(exp.Table)} - cte_names
    if not tables:
        raise ValueError("SQL 未引用任何表")
    unknown = sorted(tables - set(allowed_tables))
    if unknown:
        raise ValueError(f"SQL 引用了未授权的表：{', '.join(unknown)}")
    ph_names = {str(node.this) for node in statement.walk() if isinstance(node, exp.Placeholder) and node.this is not None}
    if ph_names != set(params):
        raise ValueError("SQL 绑定参数与模板占位符不一致")
    return sql, params, is_write


def _named_to_mysql(sql: str) -> str:
    """把 :name 具名占位符转换为 pymysql 的 %(name)s（跳过字符串/标识符引号内与反斜杠转义内容）。"""
    out: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == ":":
            match = _MYSQL_NAMED.match(sql, i)
            if match:
                out.append(f"%({match.group(1)})s")
                i = match.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _conn_params(config: dict[str, Any], password: str | None, timeout_seconds: float) -> tuple[str, dict[str, Any]]:
    """从连接器 config 提取驱动专属连接参数；兼容旧数据的 host:"host:port" 写法。返回 (driver, params)。"""
    driver = str(config.get("driver") or "postgresql").lower()
    if driver not in _DIALECTS:
        raise ValueError(f"暂不支持的数据库驱动：{driver}")
    database = str(config.get("database") or "").strip()
    if driver == "sqlite":
        if not database:
            raise ValueError("SQLite 数据库文件路径未配置")
        return driver, {"database": database, "timeout": max(1.0, min(float(timeout_seconds), 30.0))}
    host = str(config.get("host") or "").strip()
    port = config.get("port")
    if (port is None or str(port) == "") and ":" in host:
        host, _, port = host.rpartition(":")
    if not host:
        raise ValueError("数据库主机未配置")
    if not password:
        raise ValueError("数据库密码（凭证）未配置")
    timeout = max(1.0, min(float(timeout_seconds), 30.0))
    if driver == "postgresql":
        return driver, {
            "user": str(config.get("username") or ""),
            "password": password,
            "host": host,
            "port": int(port or 5432),
            "database": database,
            "timeout": timeout,
        }
    # mysql
    return driver, {
        "user": str(config.get("username") or ""),
        "password": password,
        "host": host,
        "port": int(port or 3306),
        "database": database,
        "charset": "utf8mb4",
        "connect_timeout": int(timeout),
        "read_timeout": int(timeout),
        "write_timeout": int(timeout),
    }


def _open_connection(driver: str, params: dict[str, Any]):
    if driver == "postgresql":
        return pg8000.dbapi.connect(**params)
    if driver == "mysql":
        import pymysql

        return pymysql.connect(**params)
    import sqlite3

    return sqlite3.connect(**params)


def _json_safe(value: Any) -> Any:
    """行值转 JSON 安全类型（结果会写入 JSON 字段与模型上下文）。"""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _short_error(exc: Exception) -> str:
    """压缩连接/查询错误信息，避免内部细节全量外泄。"""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:200]


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


def probe_database(config: dict[str, Any], password: str | None, timeout_seconds: float = _PROBE_TIMEOUT_SECONDS) -> list[str]:
    """DB 连接器真实探测：SELECT 1 + 拉取库内表清单（排除系统表），按驱动区分清单查询。

    连接失败抛异常，由路由层转换为统一错误响应；成功返回表名列表。
    """
    driver, params = _conn_params(config, password, timeout_seconds)
    connection = _open_connection(driver, params)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        if driver == "postgresql":
            cursor.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY table_schema, table_name"
            )
            return [
                f"{schema}.{name}" if schema != "public" else name
                for schema, name in cursor.fetchall()
            ]
        if driver == "mysql":
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name"
            )
            return [row[0] for row in cursor.fetchall()]
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        connection.close()
