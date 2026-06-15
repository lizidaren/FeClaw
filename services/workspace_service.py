"""
用户工作区服务
提供工作区初始化、文件管理、记忆同步等功能
"""

import logging
import os
from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from models.database import UserWorkspace, SessionLocal, AgentProfile
from services.storage_service import get_storage_service
from services.virtual_filesystem import VirtualFileSystem


# 默认模板文件内容
DEFAULT_SOUL = """# Agent人格

你是一个智能学习助手，帮助用户解决学习问题。
"""

DEFAULT_USER = """# 用户画像

暂无信息。
"""

DEFAULT_IDENTITY = """# 身份配置

- 角色: 学习助手
- 专长: 数学题目解答、错题分析、学习规划
"""

DEFAULT_MEMORY_HEADER = """# 长期记忆
"""


def _get_vfs_for_user(user_id: str) -> VirtualFileSystem:
    """获取用户的 VFS 实例"""
    return VirtualFileSystem(user_id=str(user_id))


def _get_agent_hash_for_user(user_id: str) -> Optional[str]:
    """获取用户的默认 Agent hash"""
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(
            AgentProfile.user_id == int(user_id),
            AgentProfile.is_default == True
        ).first()
        if not agent:
            agent = db.query(AgentProfile).filter(
                AgentProfile.user_id == int(user_id)
            ).first()
        return agent.hash if agent else None
    finally:
        db.close()


def get_agent_workspace_root(agent_hash: str) -> str:
    """获取 Agent 工作区根目录（向后兼容，返回 COS 前缀信息）"""
    return get_agent_workspace_cos_prefix(agent_hash)


def get_agent_workspace_cos_prefix(agent_hash: str) -> str:
    """获取 Agent 工作区在 COS 中的路径前缀"""
    return f"feclaw/agents/{agent_hash}/workspace/"


def get_user_workspace_root(user_id: str) -> str:
    """获取用户工作区根目录（返回 COS 前缀，向后兼容）"""
    return get_user_workspace_cos_prefix(user_id)


def get_user_workspace_cos_prefix(user_id: str) -> str:
    """获取用户工作区在 COS 中的路径前缀（向后兼容）"""
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(
            AgentProfile.user_id == int(user_id),
            AgentProfile.is_default == True
        ).first()
        if not agent:
            agent = db.query(AgentProfile).filter(
                AgentProfile.user_id == int(user_id)
            ).first()
        if agent:
            return get_agent_workspace_cos_prefix(agent.hash)
        return f"feclaw/user_workspaces/{str(user_id)}/workspace/"
    finally:
        db.close()


def get_user_workspace_db(user_id: str, db: Session) -> Optional[UserWorkspace]:
    """获取用户工作区数据库记录"""
    return db.query(UserWorkspace).filter(UserWorkspace.user_id == str(user_id)).first()


def init_user_workspace(user_id: str, db: Session) -> UserWorkspace:
    """
    初始化用户工作区
    通过 VFS 在 COS 上创建目录结构和必要的文件
    """
    user_id = str(user_id)
    vfs = _get_vfs_for_user(user_id)

    # 通过 VFS 创建目录结构
    directories = [
        "agent",
        "agent/memory",
        "tmp",
        "code",
    ]

    for directory in directories:
        vfs.mkdir(f"/workspace/{directory}", parents=True)

    # 创建默认文件
    _ensure_agent_files(user_id, db)

    # 更新数据库记录
    workspace = get_user_workspace_db(user_id, db)
    if not workspace:
        workspace = UserWorkspace(
            user_id=user_id,
            cos_bucket="",
            cos_prefix=get_user_workspace_cos_prefix(user_id),
            memory_sync_at=datetime.utcnow(),
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

    return workspace


def ensure_agent_files(user_id: str, db: Session) -> dict:
    """
    确保 agent 目录下必要文件存在
    如果文件不存在则写入 COS（通过 VFS）
    """
    user_id = str(user_id)

    files = {
        "agent/soul.md": DEFAULT_SOUL,
        "agent/user.md": DEFAULT_USER,
        "agent/identity.md": DEFAULT_IDENTITY,
        "agent/memory.md": DEFAULT_MEMORY_HEADER,
    }

    created = []
    vfs = VirtualFileSystem(user_id=user_id)
    for rel_path, default_content in files.items():
        vfs_path = f"/workspace/{rel_path}"
        existing = vfs.cat(vfs_path)
        if not existing or existing.strip() == "":
            vfs.put_object(vfs_path, default_content.encode("utf-8"))
            created.append(rel_path)

    return {
        "user_id": user_id,
        "agent_dir": "/workspace/agent/",
        "files_created": created,
    }


def _ensure_agent_files(user_id: str, db: Session) -> dict:
    """内部函数：确保 agent 目录下必要文件存在"""
    return ensure_agent_files(user_id, db)


def get_user_workspace(user_id: str) -> dict:
    """
    获取用户工作区信息
    通过 VFS 返回工作区目录结构
    """
    user_id = str(user_id)
    cos_prefix = get_user_workspace_cos_prefix(user_id)

    # 通过 VFS 列出 /workspace/ 下的目录结构
    vfs = _get_vfs_for_user(user_id)
    directories = []

    try:
        # 使用 VFS 列出顶层目录
        vfs._dir_cache = {}  # 清除缓存以确保最新结果
        ls_output = vfs.ls("/workspace/", show_all=False, long_format=False)
        # 使用 find 获取所有目录
        dirs_output = vfs.find("/workspace/", find_type="d")
        if dirs_output:
            directories = [d for d in dirs_output.split("\n") if d]
    except Exception as e:
        logger.warning(f"Failed to detect workspace directories: {e}")

    exists = len(directories) > 0

    return {
        "user_id": user_id,
        "workspace_root": cos_prefix,
        "exists": exists,
        "directories": sorted(directories),
    }


def sync_daily_memory_to_memory(user_id: str, db: Session) -> dict:
    """
    合并 memory/*.md → memory.md（通过 VFS/COS）
    将每日记忆文件合并到长期记忆文件
    """
    user_id = str(user_id)
    vfs = _get_vfs_for_user(user_id)

    memory_dir_path = "/workspace/agent/memory"
    memory_file_path = "/workspace/agent/memory.md"

    # 列出记忆目录下的文件
    try:
        dir_listing = vfs.ls(memory_dir_path, show_all=False)
        daily_files = []
        if dir_listing:
            for entry in dir_listing.split("  "):
                name = entry.strip().rstrip("/")
                if name and name.endswith(".md") and name != "memory.md":
                    daily_files.append(name)
    except Exception as e:
        logger.debug(f"[Workspace] Failed to list memory dir: {e}")
        daily_files = []

    daily_files.sort()

    if not daily_files:
        return {
            "user_id": user_id,
            "status": "no_files",
            "merged_count": 0,
            "message": "no daily memory files to merge",
        }

    # 读取现有长期记忆内容
    existing_content = ""
    existing = vfs.cat(memory_file_path)
    if existing and not existing.startswith("Error"):
        existing_content = existing

    # 构建合并后的内容
    merged_content = existing_content.rstrip() + "\n\n" if existing_content else DEFAULT_MEMORY_HEADER

    # 添加新的每日记忆
    merged_content += "\n## 合并的记忆\n"
    for daily_name in daily_files:
        date_str = daily_name.replace(".md", "")
        daily_path = f"{memory_dir_path}/{daily_name}"
        daily_content = vfs.cat(daily_path)
        if daily_content and not daily_content.startswith("Error"):
            merged_content += f"\n### {date_str}\n{daily_content}\n"

    # 写入合并后的记忆
    vfs.echo(merged_content, memory_file_path, append=False)

    # 更新数据库同步时间
    workspace = get_user_workspace_db(user_id, db)
    if workspace:
        workspace.memory_sync_at = datetime.utcnow()
        db.commit()

    return {
        "user_id": user_id,
        "status": "success",
        "merged_count": len(daily_files),
        "merged_files": daily_files,
    }


def list_memory_files(user_id: str) -> List[dict]:
    """
    列出用户的所有记忆文件（通过 VFS/COS）
    """
    user_id = str(user_id)
    vfs = _get_vfs_for_user(user_id)

    memory_dir = "/workspace/agent/memory"
    files = []

    try:
        dir_listing = vfs.ls(memory_dir, show_all=False)
        if dir_listing:
            for entry in dir_listing.split("  "):
                name = entry.strip().rstrip("/")
                if name and name.endswith(".md"):
                    files.append({
                        "name": name,
                        "path": f"{memory_dir}/{name}",
                        "size": 0,
                        "modified_at": datetime.utcnow().isoformat(),
                    })
    except Exception as e:
        logger.warning(f"Failed to list memory files: {e}")

    return sorted(files, key=lambda x: x["name"])


def get_memory_content(user_id: str, filename: Optional[str] = None) -> str:
    """
    获取记忆文件内容（通过 VFS/COS）
    如果 filename 为 None，返回 memory.md 内容
    """
    user_id = str(user_id)
    vfs = _get_vfs_for_user(user_id)

    if filename:
        memory_path = f"/workspace/agent/memory/{filename}"
    else:
        memory_path = "/workspace/agent/memory.md"

    content = vfs.cat(memory_path)
    if content and not content.startswith("Error"):
        return content
    return ""


def is_workspace_initialized(user_id: str) -> bool:
    """
    检查用户工作区是否已初始化（通过 VFS/COS）
    通过检查 memory.md 是否只包含默认内容来判断
    """
    user_id = str(user_id)
    vfs = _get_vfs_for_user(user_id)

    content = vfs.cat("/workspace/agent/memory.md")
    if not content or content.startswith("Error"):
        return False

    content = content.strip()

    # 如果只包含默认 header 内容，则认为未初始化
    default_stripped = DEFAULT_MEMORY_HEADER.strip()
    if not content or content == default_stripped:
        return False

    return True


def get_workspace_dir(user_id: str) -> str:
    """获取用户 workspace COS 路径"""
    return get_user_workspace_cos_prefix(str(user_id))


def list_workspace_files(user_id: str) -> List[dict]:
    """
    递归列出 workspace 目录下所有文件（不含 agent/ 目录）
    返回文件信息（name, path, size, type, updated_at）
    使用 COS 存储
    """
    user_id = str(user_id)
    cos_prefix = get_user_workspace_cos_prefix(user_id)
    prefix = f"{cos_prefix}workspace/"

    storage = get_storage_service()
    objects = storage.list_objects(prefix)

    files = []
    for obj in objects:
        key = obj['Key']
        # 提取相对路径（去掉前缀）
        rel_path = key[len(prefix):]
        if not rel_path:  # 跳过目录本身
            continue

        filename = os.path.basename(rel_path)

        # 判断文件类型
        ext = os.path.splitext(filename)[1].lower()
        file_type = "text"
        if ext in [".py", ".js", ".ts", ".json", ".yaml", ".yml", ".xml", ".html", ".css"]:
            file_type = "code"
        elif ext in [".md", ".txt", ".rst"]:
            file_type = "text"
        elif ext in [".png", ".jpg", ".jpeg", ".gif", ".svg"]:
            file_type = "image"
        elif ext in [".pdf", ".doc", ".docx", ".xls", ".xlsx"]:
            file_type = "document"

        files.append({
            "name": filename,
            "path": rel_path,
            "size": obj['Size'],
            "type": file_type,
            "updated_at": obj['LastModified'].isoformat() if hasattr(obj['LastModified'], 'isoformat') else str(obj['LastModified']),
        })

    return sorted(files, key=lambda x: x["path"])


def get_workspace_file(user_id: str, rel_path: str) -> Optional[dict]:
    """
    获取 workspace 文件内容
    返回 None 如果文件不存在或路径非法
    使用 COS 存储
    """
    user_id = str(user_id)

    # 安全检查：防止路径遍历
    if ".." in rel_path or rel_path.startswith("/"):
        return None

    cos_prefix = get_user_workspace_cos_prefix(user_id)
    key = f"{cos_prefix}workspace/{rel_path}"

    storage = get_storage_service()
    content_bytes = storage.get_file_content(key)

    if content_bytes is None:
        return None

    try:
        content = content_bytes.decode('utf-8')
    except UnicodeDecodeError:
        content = content_bytes.decode('utf-8', errors='replace')

    return {
        "name": os.path.basename(rel_path),
        "path": rel_path,
        "content": content,
        "size": len(content_bytes),
        "updated_at": datetime.utcnow().isoformat(),  # COS 不返回修改时间，使用当前时间
    }


def update_workspace_file(user_id: str, rel_path: str, content: str) -> Optional[dict]:
    """
    更新或创建 workspace 文件
    返回 None 如果路径非法
    使用 COS 存储
    """
    user_id = str(user_id)

    # 安全检查：防止路径遍历
    if ".." in rel_path or rel_path.startswith("/"):
        return None

    cos_prefix = get_user_workspace_cos_prefix(user_id)
    key = f"{cos_prefix}workspace/{rel_path}"

    storage = get_storage_service()
    content_bytes = content.encode('utf-8')
    storage.put_object(key, content_bytes)

    return {
        "name": os.path.basename(rel_path),
        "path": rel_path,
        "size": len(content_bytes),
        "updated_at": datetime.utcnow().isoformat(),
    }


def delete_workspace_file(user_id: str, rel_path: str) -> bool:
    """
    删除 workspace 文件
    返回 False 如果路径非法
    使用 COS 存储
    """
    user_id = str(user_id)

    # 安全检查：防止路径遍历
    if ".." in rel_path or rel_path.startswith("/"):
        return False

    cos_prefix = get_user_workspace_cos_prefix(user_id)
    key = f"{cos_prefix}workspace/{rel_path}"

    storage = get_storage_service()
    return storage.delete_file_by_key(key)
