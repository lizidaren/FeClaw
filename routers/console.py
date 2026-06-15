"""
控制台 API 路由
用户管理自己的 Agent
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
import logging

from models.database import get_db, User, AgentProfile
from models.agent_profile import AgentProfile
from services.agent_jwt_service import agent_jwt_service
from services.agent_init_service import agent_init_service
from utils.auth import get_current_user
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/console", tags=["Console"])


# ==========================================
# Agent 管理
# ==========================================

@router.get("/agents")
async def list_agents(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    列出用户的所有 Agent
    """
    agents = db.query(AgentProfile).filter(
        AgentProfile.user_id == user.id
    ).order_by(AgentProfile.created_at.desc()).all()

    return JSONResponse(content={
        "status": "success",
        "agents": [
            {
                "id": agent.id,
                "hash": agent.hash,
                "name": agent.name,
                "description": agent.description or "",
                "status": agent.status,
                "is_default": agent.is_default,
                "created_at": agent.created_at.isoformat() if agent.created_at else None,
                "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None,
                "configured_at": agent.configured_at.isoformat() if agent.configured_at else None
            }
            for agent in agents
        ],
        "total": len(agents)
    })


@router.get("/status")
async def agent_status(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    用户仪表盘统计信息

    返回当前用户的对话数、Agent 数、文件数和存储用量。
    """
    from models.database import ChatHistory

    # 对话总数
    conversations = db.query(ChatHistory).filter(
        ChatHistory.user_id == user.id
    ).distinct(ChatHistory.session_id).count()

    # 可用 Agent 数
    agents = db.query(AgentProfile).filter(
        AgentProfile.user_id == user.id
    ).count()

    # 文件数
    from models.database import UploadedFile
    files = db.query(UploadedFile).filter(
        UploadedFile.user_id == user.id
    ).count()

    return JSONResponse(content={
        "conversations": conversations,
        "agents": agents,
        "files": files,
        "storage": "--"
    })


@router.post("/agents")
async def create_agent(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    创建新的 Agent
    """
    body = await request.json()
    name = body.get("name", "")

    try:
        agent = agent_init_service.create_agent(db, user.id, name=name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return JSONResponse(content={
        "status": "success",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "status": agent.status,
            "created_at": agent.created_at.isoformat() if agent.created_at else None
        }
    })


# ==========================================
# Agent 管理（基于 agent_hash）
# ==========================================

@router.get("/agents/by-hash/{agent_hash}")
async def get_agent_by_hash(
    agent_hash: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    通过 agent_hash 获取 Agent 详情
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == agent_hash,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    return JSONResponse(content={
        "status": "success",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "status": agent.status,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None
        }
    })


@router.post("/agents/by-hash/{agent_hash}/initialize")
async def initialize_agent_by_hash(
    agent_hash: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    通过 agent_hash 初始化 Agent

    创建 Agent 配置文件和 VFS 目录结构
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == agent_hash,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.status == "initialized":
        return JSONResponse(content={
            "status": "already_initialized",
            "message": "Agent already initialized",
            "agent_hash": agent.hash
        })

    # 获取可选的自定义配置
    body = await request.json() if request.headers.get("content-length") else {}
    persona = body.get("persona", None)
    tools_config = body.get("tools", None)
    agent_config = body.get("config", None)

    # 初始化 Agent
    result = agent_init_service.initialize_agent(
        db=db,
        agent=agent,
        persona=persona,
        tools_config=tools_config,
        agent_config=agent_config
    )

    return JSONResponse(content={
        "status": "success",
        "message": "Agent initialized successfully",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "status": agent.status,
            "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None
        },
        "details": result
    })


@router.get("/agents/by-hash/{agent_hash}/status")
async def get_agent_status_by_hash(
    agent_hash: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    通过 agent_hash 获取 Agent 状态

    包括 profile 文件和 VFS 目录状态
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == agent_hash,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    status_info = agent_init_service.get_agent_status(agent)

    return JSONResponse(content={
        "status": "success",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "db_status": agent.status,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None
        },
        "initialization_status": status_info
    })


@router.get("/vfs-templates")
async def get_vfs_templates():
    """
    获取 VFS 模板列表

    返回可用的 VFS 目录结构和配置模板
    """
    templates = {
        "directories": {
            "workspace": "用户工作区，存放文件和项目",
            "workspace/agent": "Agent 配置目录（自动生成）",
            "workspace/agent/memory": "长期记忆存储",
            "workspace/images": "图片存储目录",
            "public": "公共空间（平台配置）"
        },
        "config_files": {
            "persona.md": "Agent 人设配置",
            "tools.json": "工具启用/禁用配置",
            "config.json": "Agent 运行配置"
        },
        "examples": {
            "memory_example": "memory/YYYY-MM-DD.md 格式，记录每日重要信息",
            "persona_example": agent_init_service.PERSONA_TEMPLATES.get("default", {}).get("persona", "")
        }
    }

    return JSONResponse(content={
        "status": "success",
        "templates": templates
    })


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    获取 Agent 详情
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    return JSONResponse(content={
        "status": "success",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "status": agent.status,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None
        }
    })


@router.put("/agents/{agent_id}")
async def update_agent(
    agent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    更新 Agent 配置
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # 获取请求体
    body = await request.json()

    # 更新允许的字段
    if "name" in body:
        agent.name = body["name"]

    if "status" in body:
        # 只允许特定状态
        valid_statuses = ["pending", "initialized", "suspended"]
        new_status = body["status"]
        if new_status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")
        agent.status = new_status

    agent.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(agent)

    return JSONResponse(content={
        "status": "success",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "status": agent.status,
            "updated_at": agent.updated_at.isoformat() if agent.updated_at else None
        }
    })


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    删除 Agent
    
    完整清理流程：
    1. 清理数据库相关记录（会话、消息、权限等）
    2. 清理 VFS 存储数据（COS）
    3. 清理本地配置文件
    4. 删除 Agent 记录本身
    
    使用事务确保数据一致性
    """
    from services.agent_cleanup_service import agent_cleanup_service
    
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    agent_hash = agent.hash
    logger.info(f"Starting agent deletion: id={agent_id}, hash={agent_hash}, user_id={user.id}")

    try:
        # 1. 清理所有资源（数据库记录、VFS、本地文件）
        cleanup_results = agent_cleanup_service.cleanup_agent(db, agent)
        
        # 2. 删除 Agent 记录本身
        db.delete(agent)
        db.commit()
        
        logger.info(f"Agent deleted successfully: id={agent_id}, hash={agent_hash}, cleanup={cleanup_results}")
        
        return JSONResponse(content={
            "status": "success",
            "message": "Agent deleted successfully",
            "agent_hash": agent_hash,
            "cleanup_summary": {
                "database_records": cleanup_results.get("database_records", {}),
                "vfs_files_deleted": cleanup_results.get("vfs_files", {}).get("deleted_files", 0),
                "local_files_deleted": len(cleanup_results.get("local_files", {}).get("deleted_files", [])),
                "errors": cleanup_results.get("errors", [])
            }
        })
        
    except Exception as e:
        # 回滚事务
        db.rollback()
        logger.error(f"Failed to delete agent {agent_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete agent: {str(e)}")


@router.post("/agents/{agent_id}/token")
async def issue_agent_token(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    为 Agent 签发 JWT token
    用于 Agent 与外部系统交互
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # 签发 Agent JWT
    token = agent_jwt_service.issue_agent_jwt(
        user_id=user.id,
        agent_id=agent.id,
        agent_hash=agent.hash,
        permissions=["chat", "upload", "session", "websocket"]
    )

    return JSONResponse(content={
        "status": "success",
        "token": token,
        "agent_hash": agent.hash,
        "expires_in": settings.JWT_EXPIRE_HOURS * 3600  # 秒
    })


@router.post("/agents/{agent_id}/initialize")
async def initialize_agent(
    agent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    初始化 Agent

    创建 Agent 配置文件和 VFS 目录结构
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.status == "initialized":
        return JSONResponse(content={
            "status": "already_initialized",
            "message": "Agent already initialized",
            "agent_hash": agent.hash
        })

    # 获取可选的自定义配置
    body = await request.json() if request.headers.get("content-length") else {}
    persona = body.get("persona", None)
    tools_config = body.get("tools", None)
    agent_config = body.get("config", None)

    # 初始化 Agent
    result = agent_init_service.initialize_agent(
        db=db,
        agent=agent,
        persona=persona,
        tools_config=tools_config,
        agent_config=agent_config
    )

    return JSONResponse(content={
        "status": "success",
        "message": "Agent initialized successfully",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "status": agent.status,
            "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None
        },
        "details": result
    })


@router.get("/agents/{agent_id}/status")
async def get_agent_status(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    获取 Agent 状态

    包括 profile 文件和 VFS 目录状态
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    status_info = agent_init_service.get_agent_status(agent)

    return JSONResponse(content={
        "status": "success",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "db_status": agent.status,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None
        },
        "initialization_status": status_info
    })


@router.get("/agents/{agent_id}/persona")
async def get_agent_persona(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    获取 Agent persona 内容
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    persona = agent_init_service.load_agent_persona(agent.hash)

    if persona is None:
        return JSONResponse(content={
            "status": "not_found",
            "message": "Persona file not found, agent may not be initialized"
        })

    return JSONResponse(content={
        "status": "success",
        "agent_hash": agent.hash,
        "persona": persona
    })


@router.get("/agents/{agent_id}/tools")
async def get_agent_tools(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    获取 Agent 工具配置
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    tools = agent_init_service.load_agent_tools(agent.hash)

    if tools is None:
        return JSONResponse(content={
            "status": "not_found",
            "message": "Tools config not found, agent may not be initialized"
        })

    return JSONResponse(content={
        "status": "success",
        "agent_hash": agent.hash,
        "tools": tools
    })


@router.get("/agents/{agent_id}/config")
async def get_agent_config(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    获取 Agent 配置
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    config = agent_init_service.load_agent_config(agent.hash)

    if config is None:
        return JSONResponse(content={
            "status": "not_found",
            "message": "Config file not found, agent may not be initialized"
        })

    return JSONResponse(content={
        "status": "success",
        "agent_hash": agent.hash,
        "config": config
    })


@router.put("/agents/{agent_id}/config")
async def update_agent_config(
    agent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    更新 Agent 配置（persona, style, tools）

    支持更新：
    - persona: Agent 人设（Markdown 格式）
    - style: 回复风格（professional, friendly, casual, formal, creative）
    - tools: 工具配置 {"enabled": [...], "disabled": [...]}

    配置更新后会自动重新加载
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.status != "initialized":
        raise HTTPException(status_code=400, detail="Agent must be initialized before updating config")

    body = await request.json()

    results = {}
    errors = []

    # 更新 persona
    if "persona" in body:
        persona_content = body["persona"]
        if agent_init_service.save_agent_persona(agent.hash, persona_content):
            results["persona"] = "updated"
        else:
            errors.append("Failed to save persona: content cannot be empty")

    # 更新 style
    if "style" in body:
        success, error = agent_init_service.save_agent_config(agent.hash, {"style": body["style"]})
        if success:
            results["style"] = "updated"
        else:
            errors.append(error)

    # 更新 tools
    if "tools" in body:
        tools_config = body["tools"]
        success, error = agent_init_service.save_agent_tools(agent.hash, tools_config)
        if success:
            results["tools"] = "updated"
        else:
            errors.append(error)

    # 如果有任何错误，返回部分成功状态
    if errors:
        return JSONResponse(
            status_code=400,
            content={
                "status": "partial_success",
                "results": results,
                "errors": errors
            }
        )

    # 重新加载配置
    updated_config = agent_init_service.reload_agent_config(agent.hash)

    # 更新数据库中的 updated_at
    agent.updated_at = datetime.utcnow()
    db.commit()

    return JSONResponse(content={
        "status": "success",
        "agent": {
            "id": agent.id,
            "hash": agent.hash,
            "name": agent.name,
            "updated_at": agent.updated_at.isoformat() if agent.updated_at else None
        },
        "results": results,
        "config": updated_config
    })


@router.get("/agents/{agent_id}/style")
async def get_agent_style(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    获取 Agent 回复风格
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    config = agent_init_service.load_agent_config(agent.hash)
    style = config.get("style", "professional") if config else "professional"

    return JSONResponse(content={
        "status": "success",
        "agent_hash": agent.hash,
        "style": style,
        "available_styles": agent_init_service.VALID_STYLES
    })


# ==========================================
# Agent Session 管理
# ==========================================

@router.get("/agents/{agent_id}/sessions")
async def list_agent_sessions(
    agent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    """
    列出 Agent 的会话记录
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.id == agent_id,
        AgentProfile.user_id == user.id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # 获取会话记录（使用 ConversationSession 表）
    from models.database import ConversationSession

    sessions = db.query(ConversationSession).filter(
        ConversationSession.user_id == user.id,
        ConversationSession.agent_hash == agent.hash
    ).order_by(ConversationSession.updated_at.desc()).offset(offset).limit(limit).all()

    return JSONResponse(content={
        "status": "success",
        "sessions": [
            {
                "id": session.id,
                "session_id": session.session_id,
                "topic": session.topic,
                "message_count": session.message_count,
                "created_at": session.created_at.isoformat() if session.created_at else None,
                "updated_at": session.updated_at.isoformat() if session.updated_at else None,
                "is_archived": session.is_archived
            }
            for session in sessions
        ],
        "total": len(sessions)
    })


# ==========================================
# 用户信息
# ==========================================

@router.get("/user")
async def get_console_user(
    user: User = Depends(get_current_user)
):
    """
    获取控制台用户信息
    """
    return JSONResponse(content={
        "status": "success",
        "user": {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat() if user.created_at else None
        }
    })


# ==========================================
# Templates
# ==========================================

@router.get("/templates")
async def get_persona_templates():
    """
    获取 persona 预设模板列表
    """
    templates = agent_init_service.get_persona_templates()

    # 转换为前端友好的格式
    template_list = [
        {
            "id": key,
            "name": template["name"],
            "description": template["description"],
            "persona": template["persona"]
        }
        for key, template in templates.items()
    ]

    return JSONResponse(content={
        "status": "success",
        "templates": template_list
    })


@router.get("/tools")
async def get_available_tools():
    """
    获取可用工具列表（带分组）
    """
    # 定义工具分组
    tool_groups = [
        {
            "id": "file",
            "name": "文件操作",
            "tools": [
                {"id": "file_read", "name": "读取文件", "description": "读取 VFS 中的文件内容"},
                {"id": "file_write", "name": "写入文件", "description": "向 VFS 写入文件内容"},
                {"id": "file_list", "name": "列出文件", "description": "列出目录中的文件"},
                {"id": "file_delete", "name": "删除文件", "description": "删除 VFS 中的文件"}
            ]
        },
        {
            "id": "execution",
            "name": "代码执行",
            "tools": [
                {"id": "bash", "name": "Bash 命令", "description": "执行 bash 命令"},
                {"id": "python_background", "name": "Python 后台任务", "description": "启动后台 Python 任务"},
                {"id": "python_task_list", "name": "任务列表", "description": "列出后台任务"},
                {"id": "python_task_stop", "name": "停止任务", "description": "停止后台任务"},
                {"id": "python_task_output", "name": "获取任务输出", "description": "获取后台任务输出"}
            ]
        },
        {
            "id": "search",
            "name": "搜索与网络",
            "tools": [
                {"id": "web_search", "name": "网页搜索", "description": "搜索网页内容"}
            ]
        },
        {
            "id": "session",
            "name": "会话管理",
            "tools": [
                {"id": "schedule_reminder", "name": "定时提醒", "description": "设置定时提醒"},
                {"id": "list_reminders", "name": "列出提醒", "description": "列出所有提醒"},
                {"id": "cancel_reminder", "name": "取消提醒", "description": "取消定时提醒"},
                {"id": "end_conversation", "name": "结束对话", "description": "结束当前对话"},
                {"id": "edit", "name": "编辑文件", "description": "精确替换文件中的字符串"},
                {"id": "list_conversations", "name": "列出对话", "description": "列出历史对话"},
                {"id": "load_conversation", "name": "加载对话", "description": "加载历史对话"},
                {"id": "generate_summary", "name": "生成摘要", "description": "生成对话摘要"},
                {"id": "search_sessions", "name": "搜索会话", "description": "搜索历史会话"},
                {"id": "auto_suggest_session", "name": "自动建议", "description": "自动建议相关会话"}
            ]
        },
        {
            "id": "subagent",
            "name": "子Agent",
            "tools": [
                {"id": "spawn_subagent", "name": "启动子Agent", "description": "启动子 Agent 处理任务"},
                {"id": "list_subagent_roles", "name": "列出角色", "description": "列出可用的子 Agent 角色"}
            ]
        },
        {
            "id": "utility",
            "name": "实用工具",
            "tools": [
                {"id": "create_share_link", "name": "创建分享链接", "description": "创建文件分享链接"},
                {"id": "generate_totp", "name": "生成 TOTP", "description": "生成一次性密码"}
            ]
        }
    ]

    return JSONResponse(content={
        "status": "success",
        "tool_groups": tool_groups,
        "available_styles": agent_init_service.VALID_STYLES
    })


# ==========================================
# 公共配置 API
# ==========================================

_SENSITIVE_KEYWORDS = ("SECRET", "KEY", "PASSWORD")

@router.get("/public-config")
async def get_public_config():
    """
    返回非敏感配置，方便前端读取

    敏感字段（包含 SECRET、KEY、PASSWORD 的字段）不会暴露。
    """
    config_data = {}
    for field_name in dir(settings):
        if field_name.startswith("_"):
            continue
        if any(kw in field_name.upper() for kw in _SENSITIVE_KEYWORDS):
            continue
        value = getattr(settings, field_name, None)
        if callable(value):
            continue
        try:
            # 确保值是 JSON 可序列化的
            config_data[field_name] = value
        except (TypeError, ValueError):
            continue

    return JSONResponse(content={
        "status": "success",
        "config": config_data
    })