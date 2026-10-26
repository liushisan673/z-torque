"""audit_export_archive 插件入口（v1.3 M2）。

装配期职责：
1. 读取并校验配置（缺配置不炸服务，非法配置装配期 fail fast）；
2. 恢复遗留任务（进程重启前处于 PENDING/RUNNING 的导出任务一律 FAILED）；
3. 经 `sdk.publish` 把两个领域服务交给装配根，main.py 转挂 app.state 供受控 API 使用。

插件不做 HTTP 暴露、不持有请求会话：API 层只调用服务方法，权限与范围判定在 API 层完成，
服务层再次以 tenant_id 收敛（纵深防御，任何一侧漏判都不会跨租户读审计）。
"""

from __future__ import annotations

from app.builtin_plugins.audit_export_archive.archiver import AuditArchiveService
from app.builtin_plugins.audit_export_archive.config import AuditExportConfig
from app.builtin_plugins.audit_export_archive.jobs import AuditExportService
from app.plugin_sdk import AUDIT_READ_PERMISSION, ENGINE_PERMISSION, AssemblyError, PluginManifest

SERVICE_AUDIT_EXPORT = "audit_export_service"
SERVICE_AUDIT_ARCHIVE = "audit_archive_service"


class AuditExportArchivePlugin:
    manifest = PluginManifest(
        name="audit_export_archive",
        description="经内核只读缝消费审计数据，提供导出与冷归档；不关闭、不绕过审计写入",
        provides=(),
        requires=("object.store", "secret.provider", "observability.sink"),
        # 仅自有 zr_audit_export_* / zr_audit_archive_* 表；审计事件数据只走 sdk.read_audit
        permissions=(ENGINE_PERMISSION, AUDIT_READ_PERMISSION),
        read_audit=True,
        # 结构化 JSON Schema：装配期按 Settings 投影校验（类型/枚举/必填）；
        # 数值边界与跨字段约束（chunk <= max_rows、strict 模式需密钥）在 AuditExportConfig.validate()。
        config_schema={
            "type": "object",
            "properties": {
                "audit_export_max_rows": {
                    "type": "integer",
                    "default": 100000,
                    "description": "单 Job 行数上限，超出即失败（不产出截断件）",
                },
                "audit_export_chunk_rows": {
                    "type": "integer",
                    "default": 1000,
                    "description": "keyset 每批行数 = 分片粒度",
                },
                "audit_export_max_concurrency": {
                    "type": "integer",
                    "default": 1,
                    "description": "导出线程池大小",
                },
                "audit_export_retry_limit": {
                    "type": "integer",
                    "default": 3,
                    "description": "单分片重试次数",
                },
                "audit_export_hot_retention_days": {
                    "type": "integer",
                    "default": 90,
                    "description": "热窗口保留天数",
                },
                "audit_export_formats": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["jsonl", "csv"]},
                    "default": ["jsonl"],
                    "description": "允许的导出格式",
                },
                "audit_export_exclude_own_actions": {
                    "type": "boolean",
                    "default": True,
                    "description": "导出排除 AUDIT_* 自身事件（防递归）",
                },
                "audit_export_redaction_profile": {
                    "type": "string",
                    "enum": ["standard", "strict"],
                    "default": "standard",
                    "description": "脱敏档位；strict 下主体 id 走 HMAC",
                },
                "audit_archive_prefix": {
                    "type": "string",
                    "default": "audit-archive",
                    "description": "冷副本对象键前缀",
                },
                "audit_archive_encryption_enabled": {
                    "type": "boolean",
                    "default": False,
                    "description": "冷副本 AES-256-GCM 加密",
                },
                "audit_archive_secret_ref": {
                    "type": "string",
                    "default": "",
                    "description": "当前密钥的 secret:// 密文引用（新写入恒用这把）",
                },
                "audit_archive_previous_secret_ref": {
                    "type": "string",
                    "default": "",
                    "description": "轮换过渡期上一把密钥的只读引用（须与当前引用不同）",
                },
                "audit_archive_schedule_hours": {
                    "type": "number",
                    "default": 24.0,
                    "description": "定时归档周期（小时）",
                },
            },
            "additionalProperties": False,
        },
    )

    def contribute(self, sdk) -> None:
        try:
            config = AuditExportConfig.from_settings(sdk.settings)
            config.validate()
        except ValueError as exc:
            raise AssemblyError(f"plugin audit_export_archive has invalid configuration: {exc}") from exc
        # 熔断/配额实例由内核经 SDK 注入（全装配期唯一），插件不自行构造
        guard = sdk.guard
        export_service = AuditExportService(sdk, config, guard=guard)
        archive_service = AuditArchiveService(sdk, config, guard=guard)
        sdk.publish(SERVICE_AUDIT_EXPORT, export_service)
        sdk.publish(SERVICE_AUDIT_ARCHIVE, archive_service)
        # 恢复遗留任务并把执行器拉起；shutdown 由装配根统一调用
        export_service.start()
        archive_service.start_scheduler()

    def shutdown(self, objects) -> None:  # pragma: no cover - lifespan 收尾
        export_service = objects.get(SERVICE_AUDIT_EXPORT)
        archive_service = objects.get(SERVICE_AUDIT_ARCHIVE)
        if export_service is not None:
            export_service.shutdown()
        if archive_service is not None:
            archive_service.shutdown()
