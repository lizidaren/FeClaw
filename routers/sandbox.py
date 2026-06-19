"""
Sandbox API 路由
POST /api/sandbox/execute
GET  /api/sandbox/status
POST /api/sandbox/{sandbox_id}/stop

内部 VFS API（子进程通过 httpx 调用）:
GET  /api/sandbox/vfs/file?path=
PUT  /api/sandbox/vfs/file
GET  /api/sandbox/vfs/listdir?path=
GET  /api/sandbox/vfs/stat?path=
POST /api/sandbox/vfs/mkdir
POST /api/sandbox/vfs/rename
DELETE /api/sandbox/vfs/file?path=
DELETE /api/sandbox/vfs/dir?path=
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from models.database import User
from utils.auth import get_current_user
from typing import Optional

from services.sandbox_manager import (
    SandboxManager, _global_concurrency_limiter, validate_sandbox_token
)
from services.virtual_filesystem import VirtualFileSystem
from models.database import SessionLocal, AgentProfile
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


# ============================================================================
# Request Models
# ============================================================================

class ExecuteRequest(BaseModel):
    code: str
    agent_hash: str = ""
    timeout: Optional[int] = None
    parallel: Optional[bool] = None
    lock_behavior: Optional[str] = None


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    sandbox_id: str
    timed_out: bool = False


class StatusResponse(BaseModel):
    running: int
    max: int
    queue: int
    total: int


# ============================================================================
# Helpers
# ============================================================================

# 缓存 SandboxManager 实例（按 agent_hash）
_sandbox_managers: dict = {}


def _get_sandbox_manager(agent_hash: str) -> SandboxManager:
    """获取或创建 SandboxManager 实例"""
    # 已有缓存直接返回
    if agent_hash in _sandbox_managers:
        return _sandbox_managers[agent_hash]

    # 通过 agent_hash 获取 user_id
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(
            AgentProfile.hash == agent_hash
        ).first()

        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_hash}")

        user_id = str(agent.user_id)

        # 获取或创建 VFS（每个 agent 有独立 base_path）
        vfs = VirtualFileSystem(user_id=user_id, agent_hash=agent_hash)

        # 按 agent_hash 缓存
        _sandbox_managers[agent_hash] = SandboxManager(vfs, user_id)

        return _sandbox_managers[agent_hash]

    finally:
        db.close()


# ============================================================================
# Public Endpoints
# ============================================================================

@router.post("/execute", response_model=ExecuteResponse)
async def sandbox_execute(req: ExecuteRequest, user: User = Depends(get_current_user)):
    """
    执行 Python 代码（在安全沙箱中）

    Body:
        code: Python 代码
        agent_hash: Agent 4 位 hash
        timeout: 超时秒数（可选）
        parallel: 是否允许多个并行 sandbox（可选）
        lock_behavior: 文件锁行为 "eagain" | "wait_3s"（可选）
    """
    if not req.code or not req.code.strip():
        return ExecuteResponse(
            stdout="", stderr="Error: Empty code", exit_code=1, sandbox_id=""
        )

    # 获取 SandboxManager
    try:
        manager = _get_sandbox_manager(req.agent_hash)
    except HTTPException:
        # 无 agent_hash 时，尝试默认
        manager = None
        if not req.agent_hash:
            return ExecuteResponse(
                stdout="", stderr="Error: agent_hash is required",
                exit_code=1, sandbox_id=""
            )

    # 获取 Agent 配置（parallel_sandbox, lock_behavior）
    parallel = req.parallel
    lock_behavior = req.lock_behavior or "wait_3s"

    if req.agent_hash and (req.parallel is None or req.lock_behavior is None):
        db = SessionLocal()
        try:
            agent = db.query(AgentProfile).filter(
                AgentProfile.hash == req.agent_hash
            ).first()
            if agent:
                if req.parallel is None:
                    parallel = agent.parallel_sandbox
                if req.lock_behavior is None:
                    lock_behavior = agent.lock_behavior
        finally:
            db.close()

    # 执行（在独立线程中执行，避免阻塞事件循环）
    import asyncio
    result = await asyncio.to_thread(
        manager.exec_code,
        code=req.code,
        timeout=req.timeout,
        parallel_sandbox=parallel or False,
        lock_behavior=lock_behavior
    )

    return ExecuteResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        sandbox_id=result.sandbox_id,
        timed_out=result.timed_out,
    )


@router.get("/status", response_model=StatusResponse)
async def sandbox_status(user: User = Depends(get_current_user)):
    """获取沙箱状态"""
    return StatusResponse(
        running=_global_concurrency_limiter.running_count,
        max=_global_concurrency_limiter.max_concurrent,
        queue=_global_concurrency_limiter.queue_length,
        total=_global_concurrency_limiter.total_running,
    )


@router.post("/{sandbox_id}/stop")
async def sandbox_stop(sandbox_id: str, user: User = Depends(get_current_user)):
    """停止后台沙箱任务"""
    # 遍历所有 manager 查找任务
    for manager in _sandbox_managers.values():
        if manager.stop_background(sandbox_id):
            return {"stopped": True}
    return {"stopped": False, "error": "Task not found"}


# ============================================================================
# Internal VFS API (called by subprocess via httpx)
# ============================================================================

from pydantic import BaseModel as VFSModel


class VFSFilePutData(VFSModel):
    path: str
    content: str = ""  # base64 encoded
    mode: str = "upload"


class VFSMkdirData(VFSModel):
    path: str
    parents: bool = False


class VFSRenameData(VFSModel):
    src: str
    dst: str


def _get_manager_from_request(req: Request) -> Optional[SandboxManager]:
    """从请求参数获取对应的 SandboxManager（按 agent_hash），带 token 验证"""
    agent_hash = req.query_params.get("agent_hash")
    token = req.query_params.get("token")

    # 验证 token
    if not agent_hash or not token:
        return None
    if not validate_sandbox_token(agent_hash, token):
        return None
    if agent_hash in _sandbox_managers:
        return _sandbox_managers[agent_hash]
    return None


@router.get("/vfs/file")
async def vfs_file_get(path: str, request: Request):
    """内部: 读取 VFS 文件"""
    manager = _get_manager_from_request(request)
    if not manager:
        return {"exists": False, "error": "No sandbox manager available"}
    result = manager._vfs_file_handler(path, {}, {})
    return result


@router.put("/vfs/file")
async def vfs_file_put(data: VFSFilePutData, request: Request):
    """内部: 写入 VFS 文件"""
    manager = _get_manager_from_request(request)
    if not manager:
        return {"error": "No sandbox manager available"}
    result = manager._vfs_file_handler(
        data.path, {},
        {"mode": data.mode, "content": data.content}
    )
    return result


@router.delete("/vfs/file")
async def vfs_file_delete(path: str, request: Request):
    """内部: 删除 VFS 文件"""
    manager = _get_manager_from_request(request)
    if not manager:
        return {"error": "No sandbox manager available"}
    cos_key, err = manager.vfs._resolve_path(path)
    if err:
        return {"error": err}
    manager.vfs.storage.delete_file_by_key(cos_key)
    manager.meta_cache.invalidate_dir(
        cos_key.rsplit("/", 1)[0] if "/" in cos_key else ""
    )
    return {"ok": True}


@router.get("/vfs/listdir")
async def vfs_listdir(path: str, request: Request):
    """内部: 列出 VFS 目录"""
    manager = _get_manager_from_request(request)
    if not manager:
        return {"entries": [], "error": "No sandbox manager available"}
    result = manager._vfs_listdir_handler(path)
    return result


@router.get("/vfs/stat")
async def vfs_stat(path: str, request: Request):
    """内部: VFS stat"""
    manager = _get_manager_from_request(request)
    if not manager:
        return {"error": "No sandbox manager available"}
    result = manager._vfs_stat_handler(path)
    return result


@router.post("/vfs/mkdir")
async def vfs_mkdir(data: VFSMkdirData, request: Request):
    """内部: 创建 VFS 目录"""
    manager = _get_manager_from_request(request)
    if not manager:
        return {"error": "No sandbox manager available"}
    result = manager._vfs_mkdir_handler(data.path, {"parents": data.parents})
    return result


@router.post("/vfs/rename")
async def vfs_rename(data: VFSRenameData, request: Request):
    """内部: 重命名 VFS 文件/目录"""
    manager = _get_manager_from_request(request)
    if not manager:
        return {"error": "No sandbox manager available"}
    result = manager._vfs_rename_handler(data.src, data.dst)
    return result


@router.delete("/vfs/dir")
async def vfs_dir_delete(path: str, recursive: bool = False, request: Request = None):
    """内部: 删除 VFS 目录"""
    manager = _get_manager_from_request(request)
    if not manager:
        return {"error": "No sandbox manager available"}
    params = {"recursive": "true" if recursive else "false"}
    result = manager._vfs_dir_handler(path, params)
    return result
