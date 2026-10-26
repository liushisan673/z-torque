"""Alembic autogenerate 的类型比较策略。

UUID 的存储形态在项目里并不统一，语义却相同：

- 模型侧绝大多数列用 ``GUID``（TypeDecorator，impl 为 ``CHAR(36)``），
  少数列由 ``Mapped[UUID]`` 隐式映射成 ``Uuid()``；
- 早期迁移写 ``sa.String(36)``，SQLite 落成 ``VARCHAR(36)``；
  PostgreSQL 上个别列是原生 ``uuid``。

若按类型名逐项比较，``alembic check`` 会长期输出几十条
"Detected type change from VARCHAR(36) to GUID(36)" 之类噪音，
既不能作为 CI 门禁，也会掩盖真实的列变更。这里把上述形态归为一族，
族内比较视为一致；族外仍走 Alembic 默认逻辑。

放在 ``app.db`` 而非 ``alembic/env.py``，是为了让 ``tests`` 能用与 CLI
完全相同的比较策略断言「迁移 == 模型」。
"""

from sqlalchemy import CHAR, String, Uuid

from app.db.base import GUID

_UUID_STORAGE_LENGTH = 36
_UUID_DIALECT_TYPE_NAMES = {"UUID", "GUID"}


def is_uuid_like(type_) -> bool:
    """判断是否为 UUID 的某种存储形态。"""
    if type_ is None:
        return False
    if isinstance(type_, (Uuid, GUID)):
        return True
    if isinstance(type_, (CHAR, String)) and getattr(type_, "length", None) == _UUID_STORAGE_LENGTH:
        return True
    # 方言私有类型（如 PG 的 UUID）按类名兜底
    return type(type_).__name__.upper() in _UUID_DIALECT_TYPE_NAMES


def compare_type(_context, _inspected_column, _metadata_column, inspected_type, metadata_type):
    """UUID 族内部返回 False（视为一致），其余返回 None（交回默认逻辑）。"""
    if is_uuid_like(inspected_type) and is_uuid_like(metadata_type):
        return False
    return None
