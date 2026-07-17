"""
Agent 配置 API

POST /api/agent/{agent_hash}/generate-persona - AI 生成人格配置
POST /api/agent/{agent_hash}/configure - 保存配置
"""

import json
import logging
import re
from datetime import datetime

import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session

from config import settings
from models.database import get_db, AgentProfile, AgentConfig
from services.agent_init_service import agent_init_service, DEFAULT_SOUL, DEFAULT_IDENTITY
from utils.auth import get_current_user, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Agent Config"])


# ── AI 生成的 prompt 安全红线（附加到 system_prompt 末尾） ─────
_PERSONA_SAFETY_LINES = """

## ⛔ 安全红线（严格遵守）

以下内容严禁出现在生成的配置中：
1. ⛔ 禁止编写"不知道答案也要编造/糊弄/蒙混过关"等鼓励幻觉的指令
2. ⛔ 禁止编写"欺骗用户、隐瞒真相"等不诚实行为
3. ⛔ 禁止编写"用户说什么都对、奉承用户"等谄媚行为
4. ⛔ 禁止编写"绕过系统安全限制、忽略底层规则"等越权行为
5. ⛔ 禁止编写歧视、辱骂、攻击性等有害内容

如果用户要求生成上述违规内容，友好地拒绝，告知用户这样的指令对有学习需求的用户不好，并尝试将话题引导到积极、建设性的方向上。"""


def _verify_agent_ownership(agent_hash: str, user: User, db: Session) -> AgentProfile:
    """验证 Agent 所有权并返回 AgentProfile"""
    agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if str(agent.user_id) != str(user.id):
        raise HTTPException(status_code=403, detail="无权访问")
    return agent


@router.post("/api/agent/{agent_hash}/generate-persona")
async def generate_persona(
    agent_hash: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """使用 DeepSeek 生成人格配置内容"""
    # 验证所有权
    _verify_agent_ownership(agent_hash, user, db)

    body = await request.json()
    user_description = body.get("description", "").strip()
    if not user_description:
        raise HTTPException(status_code=400, detail="请描述你想要的 Agent")

    current_soul = body.get("current_soul", "").strip()
    current_identity = body.get("current_identity", "").strip()
    current_user = body.get("current_user", "").strip()

    current_context = ""
    if current_soul or current_identity or current_user:
        current_context = (
            "\n当前已有的配置内容：\n"
            "===SOUL===\n" + current_soul + "\n"
            "===IDENTITY===\n" + current_identity + "\n"
            "===USER===\n" + current_user + "\n"
        )

    system_prompt = (
        "你是 FeClaw Agent 配置助手。你需要根据用户的描述，生成 Agent 的配置文件。\n"
        + current_context
        + "\n用户希望对配置进行以下修改：\n" + user_description + "\n\n"
        + "请生成更新后的三段内容，用 ===SOUL=== ===IDENTITY=== ===USER=== 分隔。\n\n"
        + "SOUL.md (Agent 的性格、身份、行为准则):\n"
        + "- 身份定位\n- 性格特质\n- 行为准则\n- 沟通风格\n\n"
        + "IDENTITY.md (Agent 的身份卡):\n"
        + "- 名称\n- 目标\n- 职责\n- 行为准则\n\n"
        + "USER.md (用户画像):\n"
        + "- 用户背景信息\n- 学习/使用偏好\n- 用户对 Agent 的期望\n\n"
        + "风格要求：简洁、实用，中文。"
        + _PERSONA_SAFETY_LINES
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": settings.AGENT_LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"我想要一个这样的 Agent：{user_description}"}
                    ],
                    "temperature": 0.7,
                    "max_tokens": 4096
                }
            )
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"DeepSeek API call failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI 生成失败: {str(e)}")

    # Parse sections
    soul_match = re.search(r"===SOUL===\s*(.*?)(?=\n===IDENTITY===|\Z)", content, re.DOTALL)
    identity_match = re.search(r"===IDENTITY===\s*(.*?)(?=\n===USER===|\Z)", content, re.DOTALL)
    user_match = re.search(r"===USER===\s*(.*)", content, re.DOTALL)

    soul = soul_match.group(1).strip() if soul_match else ""
    identity = identity_match.group(1).strip() if identity_match else ""
    user_content = user_match.group(1).strip() if user_match else ""

    # ── 安全校验：AI 生成的内容也要过一遍 ──
    return {
        "soul": soul,
        "identity": identity,
        "user": user_content
    }


@router.post("/api/agent/{agent_hash}/generate-persona/stream")
async def generate_persona_stream(
    agent_hash: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """使用 DeepSeek 流式生成人格配置内容，返回 SSE 流"""
    # 验证所有权
    _verify_agent_ownership(agent_hash, user, db)

    body = await request.json()
    user_description = body.get("description", "").strip()
    if not user_description:
        raise HTTPException(status_code=400, detail="请描述你想要的 Agent")

    current_soul = body.get("current_soul", "").strip()
    current_identity = body.get("current_identity", "").strip()
    current_user = body.get("current_user", "").strip()

    current_context = ""
    if current_soul or current_identity or current_user:
        current_context = (
            "\n当前已有的配置内容：\n"
            "===SOUL===\n" + current_soul + "\n"
            "===IDENTITY===\n" + current_identity + "\n"
            "===USER===\n" + current_user + "\n"
        )

    system_prompt = (
        "你是 FeClaw Agent 配置助手。你需要根据用户的描述，生成 Agent 的配置文件。\n"
        + current_context
        + "\n用户希望对配置进行以下修改：\n" + user_description + "\n\n"
        + "请生成更新后的三段内容，用 ===SOUL=== ===IDENTITY=== ===USER=== 分隔。\n\n"
        + "SOUL.md (Agent 的性格、身份、行为准则):\n"
        + "- 身份定位\n- 性格特质\n- 行为准则\n- 沟通风格\n\n"
        + "IDENTITY.md (Agent 的身份卡):\n"
        + "- 名称\n- 目标\n- 职责\n- 行为准则\n\n"
        + "USER.md (用户画像):\n"
        + "- 用户背景信息\n- 学习/使用偏好\n- 用户对 Agent 的期望\n\n"
        + "风格要求：简洁、实用，中文。"
        + _PERSONA_SAFETY_LINES
    )

    from fastapi.responses import StreamingResponse

    async def event_stream():
        buffer = ""
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                        "POST",
                        "https://api.deepseek.com/chat/completions",
                        headers={
                            "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": settings.AGENT_LLM_MODEL,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": "我想要一个这样的 Agent：" + user_description}
                            ],
                            "temperature": 0.7,
                            "max_tokens": 4096,
                            "stream": True
                        }
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str.strip() == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data_str)
                                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                    if delta:
                                        buffer += delta
                                        yield f"data: {json.dumps({'content': delta})}\n\n"
                                except json.JSONDecodeError:
                                    continue

            # 发送完成事件，附带解析结果
            soul_match = re.search(r"===SOUL===\s*(.*?)(?=\n===IDENTITY===|\Z)", buffer, re.DOTALL)
            identity_match = re.search(r"===IDENTITY===\s*(.*?)(?=\n===USER===|\Z)", buffer, re.DOTALL)
            user_match = re.search(r"===USER===\s*(.*)", buffer, re.DOTALL)
            yield f"data: {json.dumps({'done': True, 'parsed': {
                'soul': soul_match.group(1).strip() if soul_match else '',
                'identity': identity_match.group(1).strip() if identity_match else '',
                'user': user_match.group(1).strip() if user_match else ''
            }})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@router.get("/api/agent/{agent_hash}/config")
async def load_agent_config(
    agent_hash: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """加载 Agent 配置（soul / identity / user / persona = 全部从 VFS/COS 读取）"""

    # Verify agent ownership（agent_init_service 已经包含验证）"""
    _verify_agent_ownership(agent_hash, user, db)

    # 从 VFS (COS) 读取 soul/identity/user，AgentConfig 不再维护这些副本
    from services.agent_tools_service import AgentToolsService
    svc = AgentToolsService(agent_hash)

    soul = DEFAULT_SOUL
    identity = DEFAULT_IDENTITY
    user_content = ""

    try:
        soul_raw = svc.vfs.read_file("/workspace/agent/soul.md")
        if soul_raw:
            soul = soul_raw
    except Exception:
        pass

    try:
        id_raw = svc.vfs.read_file("/workspace/agent/identity.md")
        if id_raw:
            identity = id_raw
    except Exception:
        pass

    try:
        usr_raw = svc.vfs.read_file("/workspace/agent/user.md")
        if usr_raw:
            user_content = usr_raw
    except Exception:
        pass

    persona = agent_init_service.load_agent_persona(agent_hash) or ""

    return {
        "status": "success",
        "soul": soul,
        "identity": identity,
        "user": user_content,
        "persona": persona
    }


@router.post("/api/agent/{agent_hash}/configure")
async def save_agent_config(
    agent_hash: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """保存 Agent 配置（soul / identity / user / bootstrap + persona merge）"""
    agent = _verify_agent_ownership(agent_hash, user, db)

    body = await request.json()
    soul = body.get("soul", "").strip()
    identity = body.get("identity", "").strip()
    user_content = body.get("user", "").strip()

    # Default BOOTSTRAP content (not user-editable)
    default_bootstrap = """# BOOTSTRAP.md

你刚刚被创建，正在与用户进行初次交流。请按以下步骤进行初始化：

1. 明确你的身份和人格（读取 soul.md 和 identity.md 了解你自己）
2. 了解用户（读取 user.md 了解用户偏好和背景）
3. 和用户进行友好交流，让用户熟悉你的能力
4. 当用户对你的表现感到满意后，删除本文件（bootstrap.md）

祝你与用户相处愉快！"""

    # Build merged persona with section markers
    persona_parts = []
    if soul:
        persona_parts.append(f"===SOUL===\n{soul}")
    if identity:
        persona_parts.append(f"===IDENTITY===\n{identity}")
    if user_content:
        persona_parts.append(f"===USER===\n{user_content}")
    persona_parts.append(f"===BOOTSTRAP===\n{default_bootstrap}")
    persona = "\n\n".join(persona_parts)

    # Write VFS files (权威源——Agent 运行时从此读取)
    from services.file_storage import create_file_storage
    from config import settings
    storage = create_file_storage()
    # COS 存储时有 feclaw/ 前缀用于路径隔离，本地存储无此前缀
    _cos_prefix = "feclaw/" if getattr(settings, "TENCENT_COS_SECRET_ID", None) else ""
    prefix = f"{_cos_prefix}agents/{agent_hash}/workspace/agent/"
    try:
        storage.put_object(f"{prefix}soul.md", soul.encode("utf-8"))
        storage.put_object(f"{prefix}identity.md", identity.encode("utf-8"))
        storage.put_object(f"{prefix}user.md", user_content.encode("utf-8"))
        storage.put_object(f"{prefix}BOOTSTRAP.md", default_bootstrap.encode("utf-8"))
    except Exception as write_err:
        logger.error(f"VFS write failed for agent {agent_hash}: {write_err}")
        raise HTTPException(status_code=502, detail=f"文件写入失败: {str(write_err)}")

    # Note: soul/identity/user 不再写入 AgentConfig。
    # Agent 运行时全部从 COS 读取 (chat_service.py, agent_executor.py)。
    # 配置页面也改为从 VFS (COS) 读取。

    # 保存经过 AI 生成的合并 persona（仅 AgentConfig 存储，供 generate_persona API 复用）
    if persona and persona.strip():
        agent_init_service.save_agent_persona(agent_hash, persona)

    # 标记配置已完成
    agent.configured_at = datetime.utcnow()
    db.commit()  # 确保 configured_at 持久化

    # 如果 Agent 还是 pending 状态，初始化并更新为 initialized
    if agent.status == "pending":
        agent_init_service.initialize_agent(db, agent, persona=persona or None)

    return {
        "status": "success",
        "message": f"Agent {agent_hash} 配置已保存",
        "agent_hash": agent_hash
    }
