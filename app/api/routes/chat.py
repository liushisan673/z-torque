from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_security_context
from app.config import get_settings
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.model_gateway import ModelRequest
from app.kernel.ports.model_selector import snapshot_model_params
from app.kernel.security_context import SecurityContext
from app.models import Agent, AgentVersion, Conversation, Message
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_application import ensure_conversation_title, handle_chat
from app.services.context_builder import build_agent_system_prompt, build_context
from app.services.stream_service import stream_chat as stream_chat_events
from app.services.tool_mapping_builder import build_tool_mappings

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", summary="发送对话消息")
async def chat(payload: ChatRequest, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key"), ctx: SecurityContext = Depends(get_security_context), db: Session = Depends(get_db)):
    result = await handle_chat(db, ctx=ctx, agent_id=payload.agent_id, conversation_id=payload.conversation_id, content=payload.message, content_parts=payload.content_parts, idempotency_key=idempotency_key, trace_id=request.state.trace_id, model_gateway=getattr(request.app.state, "model_gateway", None), skill_gateway=getattr(request.app.state, "skill_gateway", None), short_memory=request.app.state.port_registry.get("memory.short"))
    db.commit()
    response = ChatResponse(
        # CHAT：无任务直答（无工具智能体/无智能体降级），回复文本随响应返回
        # TASK：任务已入队（PENDING→worker 执行），前端订阅 /tasks/{id}/stream 获取实时执行过程
        response_type="CHAT" if result.assistant_message else "TASK",
        conversation_id=result.user_message.conversation_id,
        user_message_id=result.user_message.id,
        assistant_message_id=result.assistant_message.id if result.assistant_message else None,
        task_id=result.task_id,
        status="COMPLETED" if result.assistant_message else "QUEUED",
        confirmation=result.pending_confirmation,
        assistant_content=result.response_content,
    )
    return success_response(request, response.model_dump(mode="json"))


@router.post("/stream", summary="发送对话消息（SSE流式）")
async def chat_stream(payload: ChatRequest, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key"), ctx: SecurityContext = Depends(get_security_context), db: Session = Depends(get_db)):
    if payload.stream is not True:
        raise ValueError("stream must be true")
    conversation = db.get(Conversation, payload.conversation_id) if payload.conversation_id else None
    if conversation is None or conversation.tenant_id != ctx.tenant_id or conversation.owner_user_id != ctx.user_id:
        raise ValueError("conversation is required for streaming chat")
    user_message = Message(tenant_id=ctx.tenant_id, conversation_id=conversation.id, sender_type="USER", content=payload.message, content_parts=payload.content_parts, status="PROCESSING")
    db.add(user_message)
    db.flush()
    ensure_conversation_title(conversation, payload.message)
    gateway = getattr(request.app.state, "model_gateway", None)
    if gateway is None:
        raise ValueError("model gateway unavailable")
    # 会话绑定智能体时注入其系统提示、绑定 skill 执行指令与工具定义（与任务链路一致）；
    # 模型流式返回纯文本=闲聊直答落库，返回 tool_calls=分流创建任务（见 stream_service.stream_chat）
    agent_version = None
    if conversation.agent_id is not None:
        agent = db.get(Agent, conversation.agent_id)
        if agent is not None and agent.current_published_version_id is not None:
            agent_version = db.get(AgentVersion, agent.current_published_version_id)
    _, tools = build_tool_mappings(db, agent_version, ctx.tenant_id)
    # 调用参数：版本快照键优先（快照语义），未设置回退全局兜底
    settings = get_settings()
    temperature, max_tokens = snapshot_model_params(agent_version.config_snapshot if agent_version is not None else None, default_temperature=settings.model_temperature, default_max_tokens=settings.model_max_tokens)
    model_request = ModelRequest(
        trace_id=request.state.trace_id,
        conversation_id=conversation.id,
        messages=build_context(db, conversation, system_prompt=build_agent_system_prompt(db, agent_version)),
        model=getattr(gateway, "default_model", "mock"),
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        stream=True,
    )
    db.commit()
    return StreamingResponse(
        stream_chat_events(gateway=gateway, request=model_request, ctx=ctx, conversation_id=conversation.id, user_message_id=str(user_message.id), idempotency_key=idempotency_key, trace_id=request.state.trace_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Trace-ID": request.state.trace_id},
    )
