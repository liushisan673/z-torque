from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_WEAK_ADMIN_PASSWORDS = {'admin123', 'admin', 'password', 'password123', '123456', '12345678', 'changeme', 'admin888'}
_MIN_ADMIN_PASSWORD_LENGTH = 12
_MIN_JWT_SECRET_LENGTH = 32


def _plain_secret(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else str(value)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'Z-Torque'
    app_env: str = 'development'
    app_host: str = '127.0.0.1'
    app_port: int = 8000
    app_debug: bool = False
    # 会话 Cookie 是否仅经 HTTPS 传输；生产默认开启（P2-17）
    cookie_secure: bool = False
    database_url: str = 'sqlite:///./data/z_torque.db'
    access_token_ttl_minutes: int = 60
    refresh_token_ttl_days: int = 7
    login_failure_limit: int = 5
    login_lock_minutes: int = 15
    jwt_secret_key: SecretStr = Field(default=..., validation_alias='JWT_SECRET_KEY')
    secret_store_key: SecretStr = Field(default=..., validation_alias='SECRET_STORE_KEY')
    secret_store_dir: Path = Path('./data/secrets')
    file_storage_dir: Path = Path('./data/files')
    max_upload_size_mb: int = 50
    # 控制台跨域来源（逗号分隔）：生产必须显式配置控制台域名，默认仅本地 vite dev
    cors_allow_origins: str = 'http://localhost:5173,http://127.0.0.1:5173'

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_allow_origins.split(',') if item.strip()]

    # 显式 Port 覆盖（P2-15）："port.name=module:factory[,port.name=...]"，
    # 解析优先级最高（显式配置 > 插件 > 内置默认）
    port_overrides: str = ''

    @property
    def port_override_map(self) -> dict[str, str]:
        overrides: dict[str, str] = {}
        for item in self.port_overrides.split(','):
            entry = item.strip()
            if not entry:
                continue
            port_name, sep, target = entry.partition('=')
            if not sep or not port_name.strip() or not target.strip():
                raise ValueError(f"invalid PORT_OVERRIDES entry: {entry!r} (expect port.name=module:factory)")
            overrides[port_name.strip()] = target.strip()
        return overrides

    context_recent_rounds: int = 5
    context_max_tokens: int = 12000
    memory_working_cache_size: int = 256
    memory_short_summary_message_threshold: int = 20
    memory_short_idle_minutes: int = 10
    memory_short_enabled: bool = True
    memory_long_enabled: bool = False
    file_max_extracted_text: int = 200000
    file_parse_timeout: float = 30.0
    log_level: str = 'INFO'
    default_parent_agent_id: str | None = None
    admin_initial_username: str = 'admin'
    admin_initial_password: SecretStr | str = 'admin123'
    delegation_enabled_global: bool = True
    delegation_max_children_per_root: int = 5
    delegation_max_concurrency: int = 3
    delegation_max_total_timeout_seconds: int = 600
    delegation_max_rounds_per_child: int = 20
    delegation_max_depth: int = 1
    delegation_default_child_timeout_seconds: int = 120
    mcp_protocol_version: str = '2025-06-18'
    mcp_timeout_seconds: float = 30
    mcp_max_response_bytes: int = 1_000_000
    mcp_allowed_hosts: list[str] = Field(default_factory=list)
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_name: str = 'mock'
    # model.selector 白名单：空 = 不限制；非空时 AgentVersion 快照绑定须命中，否则回退默认模型
    model_allowed_names: list[str] = Field(default_factory=list)
    # 全局兜底模型参数（AgentVersion 快照绑定的 temperature/max_tokens 优先，无绑定或快照未设置时生效）
    model_temperature: float | None = None
    model_max_tokens: int | None = None
    model_timeout_seconds: float = 60.0
    # 插件级熔断与配额（契约 §5.5）：threshold=连续失败熔断阈值；cooldown=熔断冷却秒（半开探测等待）；
    # quota=组件每分钟调用上限（0=不限）
    plugin_breaker_failure_threshold: int = 8
    plugin_breaker_cooldown_seconds: float = 30.0
    plugin_quota_per_minute: int = 0
    worker_concurrency: int = 10
    task_default_timeout: int = 600
    task_max_timeout: int = 3600
    task_max_model_rounds: int = 5
    task_outbox_poll_interval: float = 0.25
    task_outbox_max_attempts: int = 10
    task_event_retention: int = 90
    task_human_confirm_timeout: int = 600
    md_skills_dir: Path = Path('./md-skills')
    md_skill_watch_enabled: bool = True
    md_skill_scan_interval: float = 15.0
    md_skill_script_enabled: bool = False
    md_skill_script_timeout: float = 10.0
    md_skill_script_max_output: int = 65536
    md_skill_script_max_concurrency: int = 2
    md_skill_zip_max_entries: int = 200
    md_skill_zip_max_total_mb: int = 20
    connector_connect_timeout: float = 10.0
    connector_request_timeout: float = 30.0
    connector_max_response_bytes: int = 1_000_000
    connector_default_result_limit: int = 100
    connector_allowed_targets: list[str] = Field(default_factory=list)
    connector_allow_anonymous: bool = False
    mcp_process_restart_limit: int = 3

    # ---- v1.3 M1 沙箱加固（§4.4）----
    # 允许作为脚本 cwd 的根目录白名单。三态语义（必须可区分，否则"显式清空"无法拒绝）：
    #   None（未配置）→ 默认收窄为 [MD_SKILLS_DIR]；
    #   []（显式清空）→ 装配期 AssemblyError；
    #   非空         → 逐项 resolve 后定稿。
    sandbox_allowed_roots: list[str] | None = None
    # POSIX rlimit（CPU/AS/FSIZE/NPROC）；Windows 无 rlimit 语义，由驱动器按平台跳过并暴露在 capabilities
    sandbox_rlimit_enabled: bool = True
    sandbox_max_processes: int = 64
    sandbox_max_file_bytes: int = 16 * 1024 * 1024

    # ---- v1.3 M2 审计导出与归档（§5.4）----
    # Profile 选择：main.py 显式传给 assemble()，禁止在 assemble() 内隐式读环境变量
    assembly_profile: str = 'community'
    audit_export_dir: Path = Path('./data/audit-exports')
    audit_export_hot_retention_days: int = 90
    audit_export_max_rows: int = 100_000
    audit_export_max_concurrency: int = 1
    audit_export_chunk_rows: int = 1_000
    audit_export_retry_limit: int = 3
    audit_export_formats: list[str] = Field(default_factory=lambda: ['jsonl'])
    # 递归防护：导出默认排除 AUDIT_* 自身事件；关闭时导出审计 detail.recursive=true
    audit_export_exclude_own_actions: bool = True
    # standard / strict；strict 下主体 id 走 HMAC，密钥只在本进程内使用
    audit_export_redaction_profile: str = 'standard'
    audit_export_redaction_key: str = ''
    audit_export_max_object_bytes: int = 512 * 1024 * 1024
    # 多副本任务租约（秒）：TTL 到期即判定持有者失联；instance_id 留空自动生成
    audit_export_lease_ttl_seconds: float = 60.0
    audit_export_lease_renew_interval_seconds: float = 20.0
    audit_export_instance_id: str = ''
    audit_archive_dir: Path = Path('./data/audit-archive')
    audit_archive_prefix: str = 'audit-archive'
    audit_archive_encryption_enabled: bool = False
    # 密钥材料密文（secret.provider.encrypt 产物）；明文密钥不得出现在配置文件
    audit_archive_secret_ref: str = ''
    # 轮换过渡期：上一把密钥的密文引用，**只读解密**，新写入恒用 audit_archive_secret_ref
    audit_archive_previous_secret_ref: str = ''
    audit_archive_schedule_hours: float = 24.0

    # ---- v1.3 M3 A2A（§6.4）----
    a2a_enabled: bool = False
    a2a_local_discovery: bool = True
    # JSON 数组字符串：[{"id":..,"base_url":..,"auth_secret_name":..}]（base_url 必须 https，回环可 http）
    a2a_remote_peers: str = '[]'
    # 出站白名单；空 = 全部拒绝（remote_peers 非空而此为空 → 装配期 AssemblyError）
    a2a_allowed_peers: list[str] = Field(default_factory=list)
    a2a_max_depth: int = 2
    a2a_max_fanout: int = 5
    a2a_default_timeout_seconds: float = 120.0
    # none / user_id_header / full_context_token（仅出站携带；入站一律重新认证）
    a2a_identity_propagation: str = 'user_id_header'
    a2a_max_response_bytes: int = 1_000_000
    a2a_circuit_failure_threshold: int = 5
    a2a_circuit_cooldown_seconds: float = 30.0

    @property
    def resolved_sandbox_roots(self) -> list[str]:
        """沙箱 cwd 白名单（绝对化）。未配置 → md_skills_dir；显式空 → 空列表（由插件拒绝）。"""
        if self.sandbox_allowed_roots is None:
            return [str(self.md_skills_dir.expanduser().resolve())]
        return [str(Path(item).expanduser().resolve()) for item in self.sandbox_allowed_roots if str(item).strip()]

    @property
    def a2a_peer_configs(self) -> list[dict]:
        """远端 peer 声明（JSON）；非法 JSON 视为配置错误，装配期 fail fast。"""
        import json

        raw = (self.a2a_remote_peers or '').strip() or '[]'
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f'A2A_REMOTE_PEERS must be a JSON array: {exc}') from exc
        if not isinstance(data, list):
            raise ValueError('A2A_REMOTE_PEERS must be a JSON array')
        return [item for item in data if isinstance(item, dict)]

    def validate_runtime(self) -> None:
        if self.delegation_max_depth > 1:
            raise ValueError('delegation_max_depth must be <= 1')
        if self.app_env == 'production':
            if self.app_debug:
                raise ValueError('APP_DEBUG must be false in production')
            password = _plain_secret(self.admin_initial_password)
            if (
                len(password) < _MIN_ADMIN_PASSWORD_LENGTH
                or password.lower() in _WEAK_ADMIN_PASSWORDS
                or password.lower() == self.admin_initial_username.lower()
            ):
                raise ValueError('ADMIN_INITIAL_PASSWORD must be a strong non-default password in production')
            jwt_secret = _plain_secret(self.jwt_secret_key)
            if len(jwt_secret) < _MIN_JWT_SECRET_LENGTH or len(set(jwt_secret)) < 8:
                raise ValueError('JWT_SECRET_KEY must be at least 32 high-entropy characters in production')
        if self.md_skill_scan_interval < 1 or self.md_skill_script_timeout <= 0 or self.md_skill_script_max_output <= 0:
            raise ValueError('MD-Skill limits must be positive')
        if self.md_skill_zip_max_entries < 1 or self.md_skill_zip_max_total_mb < 1:
            raise ValueError('MD-Skill upload limits must be positive')
        if self.audit_export_chunk_rows < 1 or self.audit_export_max_rows < self.audit_export_chunk_rows:
            raise ValueError('AUDIT_EXPORT_CHUNK_ROWS must be >= 1 and <= AUDIT_EXPORT_MAX_ROWS')
        if self.audit_export_max_concurrency < 1 or self.audit_export_retry_limit < 0:
            raise ValueError('AUDIT_EXPORT_MAX_CONCURRENCY must be >= 1 and AUDIT_EXPORT_RETRY_LIMIT >= 0')
        if self.audit_export_lease_ttl_seconds < 5:
            raise ValueError('AUDIT_EXPORT_LEASE_TTL_SECONDS must be >= 5')
        if not 0 < self.audit_export_lease_renew_interval_seconds < self.audit_export_lease_ttl_seconds:
            raise ValueError('AUDIT_EXPORT_LEASE_RENEW_INTERVAL_SECONDS must be within (0, AUDIT_EXPORT_LEASE_TTL_SECONDS)')
        if self.audit_archive_schedule_hours <= 0:
            raise ValueError('AUDIT_ARCHIVE_SCHEDULE_HOURS must be positive')
        if self.audit_export_redaction_profile not in ('standard', 'strict'):
            raise ValueError("AUDIT_EXPORT_REDACTION_PROFILE must be 'standard' or 'strict'")
        if self.audit_export_redaction_profile == 'strict' and not self.audit_export_redaction_key:
            raise ValueError('AUDIT_EXPORT_REDACTION_KEY is required in strict mode')
        if self.audit_archive_encryption_enabled and not self.audit_archive_secret_ref:
            raise ValueError('AUDIT_ARCHIVE_SECRET_REF is required when archive encryption is enabled')
        if self.audit_archive_previous_secret_ref and not self.audit_archive_encryption_enabled:
            raise ValueError('AUDIT_ARCHIVE_PREVIOUS_SECRET_REF requires archive encryption to be enabled')
        if self.audit_archive_previous_secret_ref and self.audit_archive_previous_secret_ref == self.audit_archive_secret_ref:
            raise ValueError('AUDIT_ARCHIVE_PREVIOUS_SECRET_REF must differ from AUDIT_ARCHIVE_SECRET_REF')
        if self.assembly_profile not in ('community', 'enterprise'):
            raise ValueError("ASSEMBLY_PROFILE must be 'community' or 'enterprise'")
        if self.a2a_identity_propagation not in ('none', 'user_id_header', 'full_context_token'):
            raise ValueError('A2A_IDENTITY_PROPAGATION must be none / user_id_header / full_context_token')
        if not 1 <= self.a2a_max_depth <= 8:
            raise ValueError('A2A_MAX_DEPTH must be within 1..8')
        if self.a2a_max_fanout < 1:
            raise ValueError('A2A_MAX_FANOUT must be >= 1')
        if self.sandbox_max_processes < 1 or self.sandbox_max_file_bytes < 1:
            raise ValueError('sandbox resource limits must be positive')


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    settings.secret_store_dir.mkdir(parents=True, exist_ok=True)
    settings.file_storage_dir.mkdir(parents=True, exist_ok=True)
    return settings
