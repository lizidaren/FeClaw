"""
用户工作区路由
提供工作区查询、初始化、记忆同步、文件管理等API
"""

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db, User
from utils.auth import get_current_user
from services.workspace_service import (
    init_user_workspace,
    get_user_workspace,
    sync_daily_memory_to_memory,
    ensure_agent_files,
    list_memory_files,
    get_memory_content,
    list_workspace_files,
    get_workspace_file,
    update_workspace_file,
    delete_workspace_file,
)
from services.totp_service import totp_service
from services.storage_service import get_storage_service as s


router = APIRouter(prefix="/api/workspace", tags=["工作区管理"])


class WorkspaceResponse(BaseModel):
    user_id: str
    workspace_root: str
    exists: bool
    directories: list


class InitResponse(BaseModel):
    status: str
    user_id: str
    workspace_root: str
    message: str


class MemorySyncResponse(BaseModel):
    status: str
    user_id: str
    merged_count: int
    merged_files: list


class AgentFilesResponse(BaseModel):
    user_id: str
    agent_dir: str
    files_created: list


class MemoryFileResponse(BaseModel):
    name: str
    path: str
    size: int
    modified_at: str


class MemoryContentResponse(BaseModel):
    user_id: str
    filename: Optional[str]
    content: str


class WorkspaceFileResponse(BaseModel):
    name: str
    path: str
    size: int
    type: str
    updated_at: str


class WorkspaceFileListResponse(BaseModel):
    files: list


class FileContentResponse(BaseModel):
    name: str
    path: str
    content: str
    size: int
    updated_at: str


class FileUpdateRequest(BaseModel):
    content: str


class FileUpdateResponse(BaseModel):
    name: str
    path: str
    size: int
    updated_at: str


@router.get("/me", response_model=WorkspaceResponse)
async def get_workspace_info(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取用户工作区信息"""
    info = get_user_workspace(str(user.id))
    return WorkspaceResponse(**info)


@router.post("/me/init", response_model=InitResponse)
async def initialize_workspace(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """初始化用户工作区"""
    workspace = init_user_workspace(str(user.id), db)
    from services.workspace_service import get_user_workspace_root
    return InitResponse(
        status="success",
        user_id=str(user.id),
        workspace_root=get_user_workspace_root(str(user.id)),
        message="workspace initialized successfully"
    )


@router.get("/me/memory", response_model=list)
async def get_memory_files(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取用户的记忆文件列表"""
    return list_memory_files(str(user.id))


@router.get("/{user_id}/memory/content", response_model=MemoryContentResponse)
async def get_memory(
    user_id: str,
    filename: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取记忆文件内容
    如果不指定 filename，返回 memory.md 内容
    """
    # 验证用户只能访问自己的数据
    if str(current_user.id) != str(user_id):
        raise HTTPException(status_code=403, detail="无权访问其他用户的数据")
    content = get_memory_content(user_id, filename)
    return MemoryContentResponse(
        user_id=str(user_id),
        filename=filename,
        content=content
    )


@router.post("/me/memory/sync", response_model=MemorySyncResponse)
async def sync_memory(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """合并每日记忆到长期记忆"""
    result = sync_daily_memory_to_memory(str(user.id), db)
    return MemorySyncResponse(**result)


@router.post("/{user_id}/agent/ensure", response_model=AgentFilesResponse)
async def ensure_files(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """确保 agent 目录下必要文件存在"""
    # 验证用户只能操作自己的数据
    if str(current_user.id) != str(user_id):
        raise HTTPException(status_code=403, detail="无权操作其他用户的数据")
    result = ensure_agent_files(user_id, db)
    return AgentFilesResponse(**result)


@router.get("/me/files", response_model=WorkspaceFileListResponse)
async def get_workspace_files(user: User = Depends(get_current_user)):
    """列出工作区文件"""
    files = list_workspace_files(str(user.id))
    return WorkspaceFileListResponse(files=files)


@router.get("/me/file/{path:path}", response_model=FileContentResponse)
async def get_file(path: str, user: User = Depends(get_current_user)):
    """获取文件内容"""
    result = get_workspace_file(str(user.id), path)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found or invalid path")
    return FileContentResponse(**result)


@router.put("/me/file/{path:path}", response_model=FileUpdateResponse)
async def update_file(path: str, request: FileUpdateRequest, user: User = Depends(get_current_user)):
    """更新文件内容"""
    result = update_workspace_file(str(user.id), path, request.content)
    if result is None:
        raise HTTPException(status_code=400, detail="Invalid path")
    return FileUpdateResponse(**result)


@router.delete("/me/file/{path:path}")
async def delete_file(path: str, user: User = Depends(get_current_user)) -> dict:
    """删除文件"""
    success = delete_workspace_file(str(user.id), path)
    if not success:
        raise HTTPException(status_code=404, detail="File not found or invalid path")
    return {"status": "deleted", "path": path}


# ==================== TOTP 鉴权 API ====================

class TOTPGenerateResponse(BaseModel):
    code: str
    message: str
    expires_in: int  # 秒


class TOTPVerifyRequest(BaseModel):
    user_id: str
    code: str


class TOTPVerifyResponse(BaseModel):
    token: str
    expires_at: str
    user_id: int


@router.post("/totp/generate", response_model=TOTPGenerateResponse)
async def generate_totp(user: User = Depends(get_current_user)):
    """
    生成 TOTP 登录码（Agent 调用）
    
    用户向 Agent 请求登录码，Agent 调用此接口生成
    """
    code, totp_id = totp_service.generate(str(user.id))
    return TOTPGenerateResponse(
        code=code,
        message=f"您的登录码是 {code}，有效期 5 分钟。请在 https://feclaw.firstentrance.net 输入。",
        expires_in=300
    )


@router.post("/totp/verify", response_model=TOTPVerifyResponse)
async def verify_totp(request: TOTPVerifyRequest):
    """
    验证 TOTP 并签发 JWT

    静态网站调用此接口验证用户输入的登录码
    """
    # 验证 TOTP
    result = totp_service.verify_agent_totp(request.user_id, request.code)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    return TOTPVerifyResponse(**result)


# ==================== JWT 鉴权辅助函数 ====================

async def get_user_from_jwt(authorization: Optional[str] = Header(None)) -> str:
    """
    从 JWT 获取用户 ID
    
    用于静态网站的鉴权
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = authorization[7:]  # 去掉 "Bearer "
    result = totp_service.verify_jwt(token)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    return result["user_id"]


# ==================== JWT 鉴权的文件 API（静态网站用）====================

class FileInfoResponse(BaseModel):
    name: str
    path: str
    size: int
    type: str  # "file" or "dir"
    updated_at: str


class FileListResponse(BaseModel):
    path: str
    files: List[FileInfoResponse]


class UploadResponse(BaseModel):
    path: str
    size: int
    message: str


@router.get("/files", response_model=FileListResponse)
async def list_files_jwt(
    path: str = "",
    user_id: str = Depends(get_user_from_jwt)
):
    """
    列出工作区文件（JWT 鉴权）
    
    静态网站文件管理器使用
    """
    # 使用 storage_service 直接列出
    prefix = f"feclaw/user_workspaces/{user_id}/workspace/{path}".rstrip("/")
    if not prefix.endswith("/"):
        prefix += "/"
    
    objects = s().list_objects(prefix=prefix, max_keys=1000)
    
    files = []
    seen_names = set()
    
    for obj in objects:
        # 提取相对路径
        key = obj.get('Key', '')
        rel_path = key[len(prefix):]
        
        if not rel_path:
            continue
        
        # 处理子目录（只取第一级）
        if '/' in rel_path:
            dir_name = rel_path.split('/')[0]
            if dir_name and dir_name not in seen_names:
                seen_names.add(dir_name)
                files.append(FileInfoResponse(
                    name=dir_name,
                    path=f"{path}/{dir_name}".strip("/"),
                    size=0,
                    type="dir",
                    updated_at=""
                ))
        else:
            name = rel_path
            if name not in seen_names:
                seen_names.add(name)
                files.append(FileInfoResponse(
                    name=name,
                    path=f"{path}/{name}".strip("/"),
                    size=obj.get('Size', 0),
                    type="file",
                    updated_at=obj.get('LastModified', '')
                ))
    
    # 排序：目录在前，然后按名称排序
    files.sort(key=lambda x: (0 if x.type == "dir" else 1, x.name))
    
    return FileListResponse(path=path, files=files)


@router.get("/file", response_class=PlainTextResponse)
async def get_file_jwt(
    path: str,
    user_id: str = Depends(get_user_from_jwt)
):
    """
    获取文件内容（JWT 鉴权）
    """
    from services.virtual_filesystem import VirtualFileSystem
    vfs = VirtualFileSystem(user_id=user_id)
    
    content = vfs.cat(f"/workspace/{path}")
    if content is None or content.startswith("cat:"):
        raise HTTPException(status_code=404, detail="File not found")
    
    return content


@router.put("/file")
async def update_file_jwt(
    path: str,
    content: str,
    user_id: str = Depends(get_user_from_jwt)
) -> dict:
    """
    更新文件内容（JWT 鉴权）
    """
    # 直接使用 storage_service 写入
    cos_key = f"feclaw/user_workspaces/{user_id}/workspace/{path}"
    try:
        s().put_object(cos_key, content.encode('utf-8'))
        return {"status": "updated", "path": path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")


@router.post("/upload", response_model=UploadResponse)
async def upload_file_jwt(
    path: str,
    file: UploadFile = File(...),
    user_id: str = Depends(get_user_from_jwt)
):
    """
    上传文件（JWT 鉴权）
    """
    content = await file.read()
    
    # 上传到 COS
    cos_key = f"feclaw/user_workspaces/{user_id}/workspace/{path}"
    s().upload_file(cos_key, content)
    
    return UploadResponse(
        path=path,
        size=len(content),
        message="File uploaded successfully"
    )


@router.delete("/file")
async def delete_file_jwt(
    path: str,
    user_id: str = Depends(get_user_from_jwt)
) -> dict:
    """
    删除文件（JWT 鉴权）
    """
    from services.virtual_filesystem import VirtualFileSystem
    vfs = VirtualFileSystem(user_id=user_id)
    
    success = vfs.rm(f"/workspace/{path}")
    if not success:
        raise HTTPException(status_code=404, detail="File not found")
    
    return {"status": "deleted", "path": path}
