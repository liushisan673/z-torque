// 统一错误文案层：把后端英文 message 收口为中文。
//
// 背景：后端 ApiError 的 message 面向开发者（英文），直接外露会让控制台用户看到
// 「current version cannot be archived」这类句子。这里按错误码做中文映射，
// 未覆盖的错误码一律回退到调用方给出的中文兜底，保证界面上不出现英文报错。
import { ApiRequestError } from "./api";

const CODE_TEXT: Record<string, string> = {
  // ---- 认证 / 权限 ----
  AI_AUTH_FAILED: "登录状态已失效，请重新登录",
  AI_AUTH_INVALID: "登录凭证无效，请重新登录",
  AI_PERMISSION_DENIED: "你没有执行该操作的权限",
  AI_FORBIDDEN: "你没有访问该资源的权限",
  // ---- 通用 ----
  AI_RESOURCE_NOT_FOUND: "请求的资源不存在或已被删除",
  AI_NOT_FOUND: "资源不存在或已被删除",
  AI_PARAM_INVALID: "请求参数不合法，请检查后重试",
  AI_REQUEST_INVALID: "请求不合法，请检查后重试",
  AI_VALIDATION_FAILED: "内容校验未通过，请检查后重试",
  AI_CONFLICT: "存在冲突（如名称重复），请调整后重试",
  AI_INTERNAL_ERROR: "服务内部错误，请稍后重试",
  AI_SERVICE_UNAVAILABLE: "无法连接到后端服务，请检查网络与服务状态",
  AI_CAPABILITY_UNAVAILABLE: "该工具当前不可用（未装配或缺少依赖）",
  AI_RATE_LIMITED: "操作过于频繁，请稍后再试",
  AI_TIMEOUT: "操作超时，请重试",
  AI_RESOURCE_IN_USE: "该资源仍被引用，请先删除关联内容（如连接器下已登记的工具、仍被消息引用的文件）后再试",
  // ---- 智能体 / 版本 ----
  AI_AGENT_NOT_FOUND: "智能体不存在或你没有访问权限",
  AI_AGENT_VERSION_NOT_FOUND: "智能体版本不存在",
  AI_AGENT_VERSION_IS_CURRENT: "当前发布版本不可归档，请先发布其他版本",
  AI_AGENT_VERSION_IN_USE: "该版本仍有任务在执行，请等待结束或取消后重试",
  AI_AGENT_VERSION_REFERENCED: "该版本仍被其他已发布版本的委派白名单引用",
  AI_AGENT_NAME_CONFLICT: "同名智能体已存在，请换一个名称",
  AI_AGENT_IN_USE: "该智能体仍被会话引用，请先删除相关会话或将会话改绑其他智能体后再删除",
  AI_AGENT_VERSION_NOT_PUBLISHED: "该智能体没有可用的已发布版本，请先在智能体管理中发布版本",
  AI_CONVERSATION_AGENT_CONFLICT: "当前会话绑定了其他智能体，请新建会话或切回原智能体后再发送",
  // ---- 技能 / 工具 ----
  AI_SKILL_NOT_FOUND: "技能不存在或已被删除",
  AI_SKILL_VERSION_NOT_FOUND: "技能版本不存在",
  AI_SKILL_NAME_CONFLICT: "同名技能已存在，请换一个名称",
  AI_SKILL_INVALID_MANIFEST: "技能包格式不合法，请检查 SKILL.md 与目录结构",
  AI_SKILL_TOO_LARGE: "技能包过大，请精简后重试",
  AI_SKILL_PATH_INVALID: "技能包内文件路径不合法",
  AI_SKILL_FILE_TYPE_UNSUPPORTED: "技能包内包含不受支持的文件类型",
  AI_TOOL_NOT_FOUND: "工具不存在或已被删除",
  AI_TOOL_VERSION_NOT_FOUND: "工具版本不存在",
  // ---- 模型 ----
  AI_MODEL_NOT_AVAILABLE: "模型或供应商当前不可用（未启用、缺少密钥或无可用 Endpoint）",
  AI_MODEL_UPSTREAM_ERROR: "模型服务返回异常，请稍后重试",
  AI_MODEL_RESPONSE_INVALID: "模型返回内容无法解析，请重试",
  // ---- 任务 ----
  AI_TASK_NOT_FOUND: "任务不存在或已被清理",
  AI_TASK_NOT_CANCELABLE: "该任务当前不可取消（已完成或正在收尾）",
  // ---- 文件 ----
  AI_FILE_NOT_FOUND: "文件不存在或已被删除",
  AI_FILE_TYPE_UNSUPPORTED: "文件类型不受支持",
  AI_FILE_TOO_LARGE: "文件超过大小上限",
  AI_FILE_PARSE_FAILED: "文件解析失败，请确认内容格式",
  // ---- 连接器 ----
  AI_CONNECTOR_NOT_FOUND: "连接器不存在或已被删除",
  AI_CONNECTOR_TEST_FAILED: "连接测试失败，请检查地址与凭据",
  // ---- 会话 / 消息 ----
  AI_CONVERSATION_NOT_FOUND: "会话不存在或已被删除",
  AI_MESSAGE_NOT_FOUND: "消息不存在或已被删除",
  AI_CHAT_FINALIZE_FAILED: "会话内容写入失败，请重试",
};

// 判定后端 message 是否已是可读中文（少量接口会直接返回中文说明）
function isReadableChinese(text: string): boolean {
  if (!text) return false;
  return /[\u4e00-\u9fa5]/.test(text) && !/^[A-Za-z0-9_\-\s.,:()"']+$/.test(text);
}

/**
 * 把任意异常转成界面文案。
 * - 已知错误码：返回映射的中文；
 * - 未知错误码：返回调用方中文兜底（避免英文外露），并把错误码写入控制台便于排查；
 * - 非请求异常（如本地校验失败）：直接返回中文兜底。
 */
export function errorText(err: unknown, fallback: string): string {
  if (!(err instanceof ApiRequestError)) return fallback;
  const mapped = CODE_TEXT[err.code];
  if (mapped) return mapped;
  if (isReadableChinese(err.message)) return err.message;
  // 未知错误码不暴露英文原文，仅保留到控制台
  // eslint-disable-next-line no-console
  console.warn("[api] 未映射的错误码", err.code, err.message);
  return fallback;
}

/** 供需要单独展示错误码的场景使用（如运维排障面板）。 */
export function errorCode(err: unknown): string | null {
  return err instanceof ApiRequestError ? err.code : null;
}
