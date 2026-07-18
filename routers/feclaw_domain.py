"""
FeClaw 域名专用路由

用于 *.feclaw.chat 域名的简化路由：
- / → 主页
- /login → 登录页面
- /initialize → 重定向到 /login（向后兼容）
- /files → 文件管理器
- /chat → 聊天界面
- /dashboard → 控制台
- /settings → 设置页面
- /api/xxx → API 接口

所有路由都是简化路径，不带 /api/feclaw 前缀。
"""

import logging
from typing import Optional
from config import settings
from fastapi import APIRouter, Request, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func
import os
import base64

logger = logging.getLogger(__name__)

from services.totp_service import TOTPService
from models.database import SessionLocal, ConversationSession, User, get_db
import jwt as pyjwt
from models.agent_profile import AgentProfile
from models.database import WeChatBinding
from utils.auth import get_current_user, decode_jwt_token
from models.database import User as DbUser
from utils.qr import generate_qr_data_url
from services.file_storage import create_file_storage as s

router = APIRouter(tags=["FeClaw 域名路由"])

# 配置 Jinja2Templates（cache_size=0 防止多线程缓存损坏）
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
import jinja2
_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(templates_dir),
    cache_size=0,
    autoescape=jinja2.select_autoescape(),
)
templates = Jinja2Templates(env=_env)


# ==================== 辅助函数 ====================

def extract_hash_from_host(host: str) -> Optional[str]:
    """从域名提取 agent hash，如 b92d.feclaw.chat → b92d"""
    if not host:
        return None
    parts = host.split(".")
    if len(parts) >= 3 and 4 <= len(parts[0]) <= 8:
        try:
            int(parts[0], 16)
            return parts[0]
        except ValueError:
            pass
    return None


# 允许的域名后缀（防止 X-Forwarded-Host 头注入）
# 从 FECLAW_PUBLIC_URL 动态推导
# 如果 FECLAW_PUBLIC_URL=example.com，则允许 .example.com 和 example.com
def _build_allowed_suffixes():
    from config import settings
    domain = settings.FECLAW_PUBLIC_URL
    if domain:
        return [f".{domain}", domain]
    return []  # 无 PUBLIC_URL 时不进行子域名匹配

_ALLOWED_DOMAIN_SUFFIXES = _build_allowed_suffixes()


def _get_domain(request: Request) -> str:
    """获取请求域名，优先 X-Forwarded-Host（CDN 代理），回退 Host

    对 X-Forwarded-Host 做白名单校验，防止头注入攻击。
    """
    forwarded = request.headers.get("X-Forwarded-Host", "")
    if forwarded:
        domain = forwarded.split(",")[0].strip()
        # 白名单校验：域名必须以允许的后缀结尾
        if any(domain == suffix or domain.endswith(suffix) for suffix in _ALLOWED_DOMAIN_SUFFIXES):
            return domain
        return request.headers.get("host", "")
    return request.headers.get("host", "")


def get_agent_info(agent_hash: str) -> Optional[dict]:
    """获取 Agent 信息"""
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        if not agent:
            return None

        channels = []
        wechat_binding = db.query(WeChatBinding).filter(WeChatBinding.agent_hash == agent.hash).first()
        if wechat_binding:
            channels.append({
                "type": "wechat",
                "name": "微信",
                "status": "已绑定" if wechat_binding.ilink_token else "待绑定"
            })

        return {
            "hash": agent.hash,
            "name": agent.name or f"Agent {agent.hash}",
            "user_id": agent.user_id,
            "status": agent.status,
            "created_at": agent.created_at.strftime("%Y-%m-%d %H:%M") if agent.created_at else "未知",
            "initialized_at": agent.initialized_at.strftime("%Y-%m-%d %H:%M") if agent.initialized_at else None,
            "channels": channels,
            "totp_secret": agent.totp_secret
        }
    finally:
        db.close()


def _get_token_from_request(request: Request) -> Optional[str]:
    """从请求中提取 JWT token（优先 Authorization header，其次 cookie）"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]

    # 1. 主 cookie：feclaw_jwt（Platform / 本地登录）
    token = request.cookies.get("feclaw_jwt")
    if token:
        return token

    # 2. 子域名上尝试 Agent 专属 TOTP cookie
    host = _get_domain(request)
    agent_hash = extract_hash_from_host(host)
    if agent_hash:
        totp_token = request.cookies.get(f"feclaw_jwt_totp_{agent_hash}")
        if totp_token:
            return totp_token

    return request.cookies.get("platform_session")


async def get_user_from_jwt(request: Request) -> str:
    """从 JWT 获取用户 ID（支持 Authorization header 和 cookie）"""
    token = _get_token_from_request(request)
    if not token:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    result = TOTPService.verify_jwt(token)
    if not result:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # TOTP JWT 带 agent_hash，只能在对应子域名下使用
    if result.get("agent_hash"):
        host = _get_domain(request)
        sub_hash = extract_hash_from_host(host)
        if result["agent_hash"] != sub_hash:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Token scoped to agent subdomain only")

    return result["user_id"]


async def get_user_for_page(request: Request) -> Optional[str]:
    """页面路由认证：从 JWT 获取用户 ID，失败返回 None（由路由决定重定向）"""
    try:
        return await get_user_from_jwt(request)
    except Exception:
        return None


# ==================== 认证选项端点 ====================

@router.get("/api/auth/options")
async def auth_options(host: str = Query(None), request: Request = None):
    """
    返回当前域名的登录选项。
    子域名在 SSO 失败时可调用此端点获取可用的认证方式。

    Query params:
        host: 完整 hostname

    Returns:
        {
            "is_subdomain": bool,
            "agent_hash": str or null,
            "auth_methods": ["totp", "platform_login"],
            "platform_login_url": "...",
            "oauth_login_url": "..."
        }
    """
    feclaw_domain = settings.FECLAW_PUBLIC_URL
    if not host and request:
        host = _get_domain(request)

    # 未配置 FECLAW_PUBLIC_URL 时默认非子域名
    if not feclaw_domain:
        is_subdomain = False
    else:
        is_subdomain = bool(host and host.endswith(f".{feclaw_domain}") and host != feclaw_domain)
    agent_hash = extract_hash_from_host(host) if host else None

    # 检查当前是否有有效的 token
    has_session = False
    if request:
        token = _get_token_from_request(request)
        if token:
            result = TOTPService.verify_jwt(token)
            has_session = result is not None

    return {
        "host": host,
        "is_subdomain": is_subdomain,
        "agent_hash": agent_hash,
        "has_session": has_session,
        "auth_methods": ["totp", "platform_login"] if is_subdomain else ["platform_login"],
        "platform_login_url": f"https://{feclaw_domain}/login",
        "oauth_login_url": "/api/oauth/login",
        "feclaw_domain": feclaw_domain,
    }


# ==================== SSO 同步端点 ====================

@router.get("/api/auth/sync")
async def auth_sync(
    request: Request,
    redirect: str = Query("/dashboard"),
    host: str = Query(None)
):
    """
    SSO 同步端点：子域名调用此端点。
    检查根域名 cookie，如果有效则重定向回子域名并携带 token。

    Query params:
        redirect: 验证成功后重定向到的路径（如 /agent/5178 或 /dashboard）
        host: 原始请求的完整 hostname

    流程：
    1. 检查 request 的 cookie 中是否有 feclaw_jwt
    2. 如果有 && jwt 有效 → 302 到 {host}{redirect}?token=xxx
    3. 如果没有/无效 → 302 到 /login（根域名登录页，登录后会重定向回来）
    """
    token = _get_token_from_request(request)
    if token:
        from services.oauth_service import oauth_service
        result = oauth_service.verify_local_jwt(token)
        if not result:
            # 可能是 Platform 格式的 JWT（sub 是 int），尝试转换
            try:
                raw = pyjwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM], options={"verify_sub": False})
                if "sub" in raw:
                    # 重新签发为 FeClaw 兼容格式
                    result = {"sub": str(raw["sub"]), "user_id": raw["sub"]}
                    token = oauth_service.create_local_jwt(result)
            except Exception:
                result = None

        if result:
            # Cookie 有效，重定向回子域名并携带 token
            feclaw_domain = settings.FECLAW_PUBLIC_URL
            # 没有配置 FECLAW_PUBLIC_URL 时跳过严格校验，直接使用 host
            if not feclaw_domain:
                valid_host = bool(host)
            else:
                valid_host = host and host.endswith(f".{feclaw_domain}") and host != feclaw_domain
            if valid_host:
                redirect_url = f"https://{host}{redirect}"
            else:
                # 没有子域名信息，使用 redirect 路径（根域名内跳转）
                redirect_url = redirect if redirect.startswith("/") else f"/{redirect}"
            return RedirectResponse(url=f"{redirect_url}?token={token}", status_code=302)

    # Cookie 无效或不存在，重定向到根域名登录页
    return RedirectResponse(url=f"/login?redirect_to={redirect}", status_code=302)


# ==================== API 路由 ====================

class TOTPVerifyRequest(BaseModel):
    agent_hash: str
    code: str


class TOTPVerifyResponse(BaseModel):
    token: str
    agent_hash: str
    user_id: int
    expires_at: str


class AgentInfoResponse(BaseModel):
    hash: str
    name: str
    user_id: int
    status: str
    created_at: str
    initialized_at: Optional[str]
    channels: list
    totp_secret: Optional[str] = None
    totp_qr_data_url: str = ""


class FileInfo(BaseModel):
    name: str
    path: str
    size: int
    type: str
    updated_at: str


class FileListResponse(BaseModel):
    path: str
    files: list


class TOTPGenerateResponse(BaseModel):
    code: str
    agent_hash: str
    expires_in: int
    login_url: str
    qr_data_url: str = ""


@router.post("/api/totp/verify", response_model=TOTPVerifyResponse)
async def verify_totp(request: TOTPVerifyRequest):
    """验证 TOTP 并签发 JWT"""
    result = TOTPService.verify_agent_totp(request.agent_hash, request.code)
    if not result:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=401, detail="Invalid or expired TOTP code")
    return TOTPVerifyResponse(**result)


@router.post("/api/totp/generate", response_model=TOTPGenerateResponse)
async def generate_totp(request: TOTPVerifyRequest, user=Depends(get_current_user)):
    """生成当前 TOTP 码和登录链接（含二维码 data URL）"""
    result = TOTPService.generate_for_agent(request.agent_hash)
    if result is None:
        pass  # HTTPException already imported at module level as HE
        raise HE(status_code=404, detail="Agent not found")
    code, secret = result
    from utils.qr import generate_qr_data_url
    totp_uri = f"otpauth://totp/FeClaw:{request.agent_hash}?secret={secret}&issuer=FeClaw"
    return TOTPGenerateResponse(
        code=code,
        agent_hash=request.agent_hash,
        expires_in=TOTPService.VALID_WINDOWS * TOTPService.INTERVAL,
        login_url=f"https://{request.agent_hash}.{settings.FECLAW_PUBLIC_URL}/login?totp={code}" if settings.FECLAW_SUBDOMAIN_ENABLED else f"/login?totp={code}",
        qr_data_url=generate_qr_data_url(totp_uri),
    )


@router.get("/api/agent/info", response_model=AgentInfoResponse)
async def get_agent_info_api(agent_hash: str, request: Request, user: User = Depends(get_current_user)):
    """获取 Agent 信息（JWT 鉴权）"""
    # 验证用户有权访问该 Agent
    info = get_agent_info(agent_hash)
    if not info:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=404, detail="Agent not found")

    # 确保 user_id 匹配
    if str(info["user_id"]) != str(user.id):
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=403, detail="Access denied")

    # 生成 TOTP 二维码（不依赖外部 API）
    if info.get("totp_secret"):
        from utils.qr import generate_qr_data_url
        totp_uri = f"otpauth://totp/FeClaw:{agent_hash}?secret={info['totp_secret']}&issuer=FeClaw"
        info["totp_qr_data_url"] = generate_qr_data_url(totp_uri)

    return AgentInfoResponse(**info)


def _vfs_path(agent_hash: str, path: str) -> str:
    """VFS 路径 → COS key（处理 /public/ 映射到公共空间）"""
    if path == "public" or path.startswith("public/"):
        return f"feclaw/public/{path[7:] if path.startswith('public/') else ''}"
    return f"feclaw/agents/{agent_hash}/{path}"


def _list_config_keys(agent_hash: str) -> list:
    """列出 Agent 的配置 key（排除 permission=none）"""
    try:
        from models.database import AgentConfig, SessionLocal
        from sqlalchemy import or_
        db = SessionLocal()
        try:
            q = db.query(AgentConfig).filter(
                AgentConfig.permission != "none"
            ).filter(or_(
                AgentConfig.agent_hash == agent_hash,
                AgentConfig.agent_hash == None,
            ))
            keys = set()
            for c in q.all():
                k = c.key
                if k.startswith(f"agents/{agent_hash}/"):
                    k = k[len(f"agents/{agent_hash}/"):]
                keys.add(k)
            return sorted(keys)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[VFS] _list_config_keys error: {e}")
        return []


def _read_config_value(agent_hash: str, vpath: str) -> Optional[str]:
    """从数据库读取 Agent 配置值"""
    config_key = vpath
    if vpath.startswith("config/"):
        config_key = vpath[7:]
    # 尝试两种 DB key格式：
    # 1. agents/{hash}/{key}（新格式）
    # 2. {key}（旧格式/全局配置）
    candidates = [config_key, f"agents/{agent_hash}/{config_key}"] if "/" not in config_key else [config_key]
    try:
        from models.database import AgentConfig, SessionLocal
        from sqlalchemy import or_
        db = SessionLocal()
        try:
            for db_key in candidates:
                q = db.query(AgentConfig).filter(
                    AgentConfig.key == db_key,
                    AgentConfig.permission != "none"
                ).filter(or_(
                    AgentConfig.agent_hash == agent_hash,
                    AgentConfig.agent_hash == None,
                ))
                config = q.first()
                if config:
                    return config.value
            return None
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[VFS] _read_config_value error: {e}")
        return None


@router.get("/api/files", response_model=FileListResponse)
async def list_files(request: Request, path: str = "", agent_hash: str = Query(""), user: User = Depends(get_current_user)):
    """列出 VFS 文件"""
    from fastapi.responses import Response as APIResponse
    if not agent_hash:
        agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")

    # VFS 路径映射
    cos_prefix_base = f"feclaw/agents/{agent_hash}"
    public_prefix_base = "feclaw/public"

    # 根目录：合并 agent + public
    if not path:
        files = []

        # 1. Agent 目录下的子目录
        agent_prefix = f"{cos_prefix_base}/"
        objects = s().list_objects(prefix=agent_prefix, max_keys=1000) or []
        seen = set()
        for obj in objects:
            key = obj.get('Key', '')
            rel = key[len(agent_prefix):]
            if not rel:
                continue
            if rel == '.directory' or rel.startswith('.'):
                continue
            if '/' in rel:
                d = rel.split('/')[0]
                if d and d not in seen and not d.startswith('.'):
                    seen.add(d)
                    files.append(FileInfo(name=d, path=d, size=0, type="dir", updated_at=""))
            else:
                if rel not in seen and not rel.startswith('.'):
                    seen.add(rel)
                    files.append(FileInfo(name=rel, path=rel, size=obj.get('Size', 0), type="file",
                        updated_at=obj.get('LastModified', '').split('.')[0] if obj.get('LastModified') else ""))

        # 2. 检查 /public/ 是否存在（全局公共空间）
        public_objects = s().list_objects(prefix=f"{public_prefix_base}/", max_keys=1) or []
        if public_objects and 'public' not in seen:
            files.append(FileInfo(name="public", path="public", size=0, type="dir", updated_at=""))

        # 3. 检查 /config/ 虚拟配置目录是否存在
        config_keys = _list_config_keys(agent_hash)
        if config_keys and 'config' not in seen:
            files.append(FileInfo(name="config", path="config", size=0, type="dir", updated_at=""))

        files.sort(key=lambda x: (0 if x.type == "dir" else 1, x.name))
        return FileListResponse(path="", files=files)

    # 非根目录：config 虚拟配置目录
    if path == "config" or path.startswith("config/"):
        keys = _list_config_keys(agent_hash)
        if not keys:
            return FileListResponse(path=path, files=[])
        subpath = path[7:] if path.startswith("config/") else ""  # "agent/BOOTSTRAP.md"
        subprefix = f"{subpath}/" if subpath else ""
        cfiles = []
        seen = set()
        for k in keys:
            if subprefix and not k.startswith(subprefix):
                continue
            rel = k[len(subprefix):] if subprefix else k
            if not rel:
                continue
            # 有子路径的显示为目录
            if '/' in rel:
                d = rel.split('/')[0]
                if d and d not in seen:
                    seen.add(d)
                    cfiles.append(FileInfo(name=d, path=f"config/{subpath}/{d}".replace("//", "/").strip("/"),
                        size=0, type="dir", updated_at=""))
            else:
                if rel not in seen:
                    seen.add(rel)
                    cfiles.append(FileInfo(name=rel, path=f"config/{subpath}/{rel}".replace("//", "/").strip("/"),
                        size=0, type="file", updated_at=""))
        return FileListResponse(path=path, files=cfiles)

    # 非根目录：public / agent
    is_public = path == "public" or path.startswith("public/")
    if is_public:
        subpath = path[7:] if path.startswith("public/") else ""
        prefix = f"{public_prefix_base}/{subpath}".rstrip("/") + "/"
    else:
        prefix = f"{cos_prefix_base}/{path}".rstrip("/") + "/"

    objects = s().list_objects(prefix=prefix, max_keys=1000) or []
    files = []
    seen_names = set()

    for obj in objects:
        key = obj.get('Key', '')
        rel_path = key[len(prefix):]
        if not rel_path:
            continue
        rel_name = rel_path.rstrip('/')
        if rel_name == '.directory' or rel_name.startswith('.'):
            continue

        if '/' in rel_path:
            dir_name = rel_path.split('/')[0]
            if dir_name and dir_name not in seen_names and not dir_name.startswith('.'):
                seen_names.add(dir_name)
                files.append(FileInfo(name=dir_name, path=f"{path}/{dir_name}".strip("/"),
                    size=0, type="dir", updated_at=""))
        else:
            if rel_path not in seen_names and not rel_path.startswith('.'):
                seen_names.add(rel_path)
                files.append(FileInfo(name=rel_path, path=f"{path}/{rel_path}".strip("/"),
                    size=obj.get('Size', 0), type="file",
                    updated_at=obj.get('LastModified', '').split('.')[0] if obj.get('LastModified') else ""))

    return FileListResponse(path=path, files=files)


# ==================== 文件操作 API ====================

from pydantic import BaseModel as PydanticModel


class FileContentResponse(PydanticModel):
    path: str
    content: str
    size: int
    binary: bool = False


class FileUpdateRequest(PydanticModel):
    content: str


@router.get("/api/file", response_model=FileContentResponse)
async def get_file(path: str, request: Request, agent_hash: str = Query(""), user: User = Depends(get_current_user)):
    """获取文件内容"""
    # Validate path (prevent path traversal)
    import os as _os
    if _os.path.isabs(path) or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    from fastapi import HTTPException
    if not agent_hash:
        agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")

    # 处理 /config/ 虚拟配置目录
    if path == "config" or path.startswith("config/"):
        val = _read_config_value(agent_hash, path)
        if val is None:
            raise HTTPException(status_code=404, detail="Config not found")
        return FileContentResponse(path=path, content=val, size=len(val), binary=False)

    # 构建完整路径（agent 工作区路径）
    full_path = _vfs_path(agent_hash, path)

    # 读取文件内容
    content = s().get_file_content(full_path)
    if content is None:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=404, detail="File not found")

    # 检测是否为二进制文件
    import mimetypes
    mime_type, _ = mimetypes.guess_type(path)
    is_binary = mime_type and not mime_type.startswith("text/")

    if is_binary:
        # 二进制文件 → base64 编码返回
        encoded = base64.b64encode(content).decode('ascii')
        return FileContentResponse(
            path=path,
            content=encoded,
            size=len(content),
            binary=True
        )

    return FileContentResponse(
        path=path,
        content=content.decode('utf-8') if isinstance(content, bytes) else content,
        size=len(content)
    )


@router.put("/api/file")
async def update_file(path: str, body: FileUpdateRequest, req: Request, agent_hash: str = Query(""), user: User = Depends(get_current_user)) -> dict:
    """更新文件内容"""
    # Validate path (prevent path traversal)
    import os as _os
    if _os.path.isabs(path) or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    if path == "public" or path.startswith("public/"):
        raise HTTPException(status_code=403, detail="公共空间为只读，不允许修改")
    if not agent_hash:
        agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")

    # 处理 /config/ 虚拟配置目录写入
    if path == "config" or path.startswith("config/"):
        config_key = path[7:] if path.startswith("config/") else ""
        candidates = [config_key, f"agents/{agent_hash}/{config_key}"] if "/" not in config_key else [config_key]
        from models.database import AgentConfig, SessionLocal
        from sqlalchemy import or_
        db = SessionLocal()
        try:
            existing = None
            for db_key in candidates:
                q = db.query(AgentConfig).filter(
                    AgentConfig.key == db_key,
                    AgentConfig.permission != "none"
                ).filter(or_(
                    AgentConfig.agent_hash == agent_hash,
                    AgentConfig.agent_hash == None,
                ))
                existing = q.first()
                if existing:
                    break

            if not existing:
                raise HTTPException(status_code=400, detail=f"配置项 '{config_key}' 不存在，config 目录只支持修改已有配置项")

            existing.value = body.content
            existing.updated_at = __import__("datetime").datetime.utcnow()
            db.commit()

            # 同步 sr_enabled 到 AgentProfile.sr_enabled
            if config_key == "sr_enabled" and "/" not in config_key.rstrip("/"):
                from models.database import AgentProfile
                ap = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
                if ap:
                    val = body.content.lower() in ("true", "1", "yes")
                    if ap.sr_enabled != val:
                        ap.sr_enabled = val
                        db.commit()

            return {"status": "success", "path": path, "size": len(body.content)}
        except HTTPException:
            raise  # 400/404 等直接透传
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"写入配置失败: {e}")
        finally:
            db.close()

    # 构建完整路径（agent 工作区路径）
    full_path = _vfs_path(agent_hash, path)

    # 写入文件
    content_bytes = body.content.encode('utf-8')
    s().put_object(full_path, content_bytes)

    return {"status": "success", "path": path, "size": len(content_bytes)}


@router.delete("/api/file")
async def delete_file(path: str, request: Request, agent_hash: str = Query(""), user: User = Depends(get_current_user)) -> dict:
    """删除文件"""
    # Validate path (prevent path traversal)
    import os as _os
    if _os.path.isabs(path) or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    if path == "public" or path.startswith("public/") or path == "config" or path.startswith("config/"):
        raise HTTPException(status_code=403, detail=f"{'公共空间' if path.startswith('public') else '配置目录'}为只读，不允许删除")
    if not agent_hash:
        agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")
    # 构建完整路径
    full_path = _vfs_path(agent_hash, path)

    # 删除文件
    success = s().delete_file(full_path)
    if not success:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=404, detail="File not found")

    return {"status": "deleted", "path": path}


# ==================== 本地存储友好的 raw 文件流 ====================

from fastapi.responses import Response as FastAPIResponse


@router.get("/api/file/raw")
async def get_file_raw(
    path: str,
    request: Request,
    agent_hash: str = Query(""),
    user: User = Depends(get_current_user),
):
    """直接流式返回文件原始字节（用于本地存储模式下的图片/视频/音频预览）。

    行为：
    - config/ 虚拟目录：返回 JSON 文本（与 /api/file?path=config/... 一致）
    - 其他路径：返回原始字节，带上 Content-Type
    """
    import os as _os
    if _os.path.isabs(path) or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not agent_hash:
        agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")

    # config/ 走 DB
    if path == "config" or path.startswith("config/"):
        val = _read_config_value(agent_hash, path)
        if val is None:
            raise HTTPException(status_code=404, detail="Config not found")
        return FastAPIResponse(
            content=val.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
        )

    full_path = _vfs_path(agent_hash, path)
    content = s().get_file_content(full_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    import mimetypes
    mime_type, _ = mimetypes.guess_type(path)
    return FastAPIResponse(
        content=content,
        media_type=mime_type or "application/octet-stream",
    )


# ==================== 签名 URL API ====================

class SignedUrlRequest(PydanticModel):
    path: str
    operation: str = "upload"  # "upload" | "download"
    expires: int = 3600  # 有效期（秒）

class SignedUrlResponse(PydanticModel):
    url: str
    path: str
    expires_at: str
    method: str  # "PUT" | "GET"

@router.post("/api/file/signed-url", response_model=SignedUrlResponse)
async def get_signed_url(body: SignedUrlRequest, req: Request, user: User = Depends(get_current_user)):
    """
    生成签名 URL（前端直接操作 COS）

    Args:
        body: {path, operation, expires}
        req: FastAPI Request

    Returns:
        {url, path, expires_at, method}
    """
    agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")

    # 构建完整路径（agent 工作区路径）
    full_path = _vfs_path(agent_hash, body.path)

    # 安全检查：确保路径在用户工作区内
    if ".." in body.path or body.path.startswith("/"):
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=400, detail="Invalid path")

    if body.operation == "upload":
        # 上传签名 URL（PUT）
        signed_url = s().generate_presigned_put_url(full_path, body.expires)
        method = "PUT"
    elif body.operation == "download":
        # 先检查文件是否存在
        if not s().file_exists(full_path):
            raise HTTPException(status_code=404, detail="文件不存在")
        # 下载签名 URL（GET）
        public_url = s().get_object_public_url(full_path)
        signed_url = s().generate_presigned_get_url(
            public_url,
            body.expires
        )
        method = "GET"
    else:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=400, detail="Invalid operation, must be 'upload' or 'download'")

    if not signed_url:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=500, detail="Failed to generate signed URL")

    # 计算过期时间
    from datetime import datetime, timedelta
    expires_at = (datetime.utcnow() + timedelta(seconds=body.expires)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return SignedUrlResponse(
        url=signed_url,
        path=body.path,
        expires_at=expires_at,
        method=method
    )


class StsCredentialResponse(PydanticModel):
    credentials: dict
    bucket: str
    region: str
    prefix: str
    base_url: str

@router.get("/api/file/sts-credential", response_model=StsCredentialResponse)
async def get_sts_credential(request: Request, user: User = Depends(get_current_user)):
    """
    获取 STS 临时凭证（前端直接操作 COS）

    返回临时 SecretId、SecretKey、SessionToken
    前端可使用这些凭证直接操作 COS SDK
    """
    agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")

    # 使用 agent 工作区路径前缀
    agent_prefix = f"feclaw/agents/{agent_hash}/"
    result = s().generate_sts_credential(str(user.id), prefix=agent_prefix)
    if not result:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=500, detail="Failed to generate STS credential")

    return StsCredentialResponse(**result)


# ==================== 页面路由（使用新 UI 模板）==================

# 主页 - 介绍页（新液态玻璃风格）
@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """主页 - 根据域名返回不同页面"""
    host = _get_domain(request)
    logger.info(f"Home page request: host={host}")
    logger.warning(f"[DOMAIN_DEBUG] X-Forwarded-Host={request.headers.get('X-Forwarded-Host', 'N/A')}")

    # 判断是否为 Agent 子域名（如 5178.feclaw.chat）
    agent_hash = extract_hash_from_host(host)

    if agent_hash:
        # Agent 子域名：鉴权后返回 Agent 控制台
        user_id = await get_user_for_page(request)
        if not user_id:
            return RedirectResponse(url=f"/login?redirect_to=/{agent_hash}", status_code=302)
        return templates.TemplateResponse(request, "agent_dashboard.html", {"request": request, "agent_hash": agent_hash})
    else:
        # 检查是否为无效子域名（非 4 位 hex 的子域名）→ 302 回主域名
        feclaw_domain = settings.FECLAW_PUBLIC_URL
        if feclaw_domain and host and host.endswith(f".{feclaw_domain}") and host != feclaw_domain:
            logger.info(f"Invalid subdomain detected: {host}, redirecting to {feclaw_domain}")
            return RedirectResponse(url=f"https://{feclaw_domain}", status_code=302)
        # 主域名：返回介绍页（带登录状态）
        user_id = await get_user_for_page(request)
        is_logged_in = user_id is not None
        return templates.TemplateResponse(request, "index.html", {"request": request, "is_logged_in": is_logged_in})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页面"""
    host = _get_domain(request)
    agent_hash = extract_hash_from_host(host)
    is_subdomain = bool(agent_hash)
    from services.oauth_service import oauth_service
    oauth_configured = oauth_service._oauth_configured
    oauth_enabled = oauth_configured and settings.OAUTH_ENABLED
    resp = templates.TemplateResponse(request, "login.html", {
        "request": request,
        "is_subdomain": is_subdomain,
        "agent_hash": agent_hash,
        "oauth_configured": oauth_configured,
        "oauth_enabled": oauth_enabled,
        "oauth_login_url": "/api/oauth/login",
        "oauth_provider_name": settings.OAUTH_PROVIDER_NAME,
        "oauth_register_url": settings.OAUTH_PROVIDER_URL + "/register",
        "totp_strict_ownership": settings.TOTP_STRICT_OWNERSHIP,
    })
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@router.get("/initialize", response_class=HTMLResponse)
async def initialize_page(request: Request):
    """初始化页面（新 UI）"""
    return templates.TemplateResponse(request, "initialize.html", {"request": request})


@router.get("/setup", response_class=HTMLResponse)
@router.get("/setup/", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    reset: str = Query("", alias="reset"),
):
    """配置向导 / 已完成时的只读摘要页面。

    冷启动：setup_router 的路由生效（此路由在 cold-start 时不会注册）。
    正常启动：复用 setup_router 的渲染逻辑：
      - 已完成 → 渲染只读 summary
      - 未完成 / ?reset=1 → 渲染向导
    """
    from sqlalchemy.orm import Session as _Session
    from models.database import get_db as _get_db
    db: _Session = next(_get_db())
    try:
        from routers.setup import _render_setup_view
        return _render_setup_view(request=request, db=db, token="", reset=reset)
    finally:
        db.close()


@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/dashboard/", response_class=HTMLResponse)
async def dashboard_page(request: Request, agent: Optional[int] = Query(None)):
    """控制台页面（新 UI，需登录）"""
    # 兼容旧格式 /dashboard?agent=N → 302 到 /agent/{hash}
    if agent is not None:
        db = SessionLocal()
        try:
            agent_profile = db.query(AgentProfile).filter(AgentProfile.id == agent).first()
            if agent_profile:
                domain = settings.FECLAW_PUBLIC_URL
            if domain and settings.FECLAW_SUBDOMAIN_ENABLED:
                return RedirectResponse(url=f"https://{agent_profile.hash}.{domain}", status_code=302)
            return RedirectResponse(url=f"/agent/{agent_profile.hash}", status_code=302)
        finally:
            db.close()
    if not await get_user_for_page(request):
        return RedirectResponse(url="/login", status_code=302)
    host = _get_domain(request)
    agent_hash = extract_hash_from_host(host)
    # 解析 is_admin（用于在导航栏显示「管理后台」入口）
    is_admin = False
    try:
        # get_current_user 用 Depends(get_db)，手动调用需要解析 db
        from utils.auth_dependencies import _extract_global_jwt, _decode_or_none, _user_id_from_payload
        from models.database import User, get_db as _get_db_factory
        token = _extract_global_jwt(request)
        payload = _decode_or_none(token) if token else None
        uid = _user_id_from_payload(payload) if payload else None
        if uid:
            db = next(_get_db_factory())
            try:
                u = db.query(User).filter(User.id == uid).first()
                is_admin = bool(u and getattr(u, "is_admin", False))
            finally:
                db.close()
    except Exception:
        is_admin = False
    if agent_hash:
        return templates.TemplateResponse(
            request, "agent_dashboard.html",
            {"request": request, "agent_hash": agent_hash, "is_admin": is_admin},
        )
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"request": request, "agent_hash": agent_hash, "is_admin": is_admin},
    )


@router.get("/files", response_class=HTMLResponse)
@router.get("/files/", response_class=HTMLResponse)
async def files_page(request: Request, path: str = ""):
    """文件管理页面 - 已迁移到 Agent 控制台"""
    from fastapi.responses import RedirectResponse, HTMLResponse
    from fastapi import Response as FastAPIResponse
    host = _get_domain(request)
    agent_hash = extract_hash_from_host(host)
    if agent_hash:
        if not await get_user_for_page(request):
            return RedirectResponse(url="/login", status_code=302)
        rendered = templates.TemplateResponse(request, "agent_files.html", {
            "request": request,
            "agent_hash": agent_hash,
            "home_url": "/",
        })
        # CDN 不要缓存动态页面
        rendered.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0"
        rendered.headers["Pragma"] = "no-cache"
        rendered.headers["Expires"] = "0"
        return rendered
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/chat", response_class=HTMLResponse)
@router.get("/chat/", response_class=HTMLResponse)
async def chat_page(request: Request, agent: Optional[int] = Query(None)):
    """聊天页面 - 已迁移到 Agent 控制台"""
    from fastapi.responses import RedirectResponse
    # 兼容旧格式 /chat?agent=N → 302 到 /agent/{hash}/chat
    if agent is not None:
        db = SessionLocal()
        try:
            agent_profile = db.query(AgentProfile).filter(AgentProfile.id == agent).first()
            if agent_profile:
                return RedirectResponse(url=f"/agent/{agent_profile.hash}/chat", status_code=302)
        finally:
            db.close()
    host = _get_domain(request)
    agent_hash = extract_hash_from_host(host)
    if agent_hash:
        if not await get_user_for_page(request):
            return RedirectResponse(url="/login", status_code=302)
        return templates.TemplateResponse(request, "agent_chat.html", {
            "request": request,
            "agent_hash": agent_hash
        })
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/settings", response_class=HTMLResponse)
@router.get("/settings/", response_class=HTMLResponse)
async def settings_page(request: Request):
    """设置页面（新 UI，需登录）"""
    if not await get_user_for_page(request):
        return RedirectResponse(url="/login", status_code=302)
    host = _get_domain(request)
    agent_hash = extract_hash_from_host(host)

    # 解析 is_admin（用于在导航栏显示「管理后台」入口）
    is_admin = False
    try:
        from utils.auth import get_current_user_id
        uid = get_current_user_id(request)
        if uid:
            from models.database import SessionLocal, User
            db = SessionLocal()
            try:
                u = db.query(User).filter(User.id == int(uid)).first()
                is_admin = bool(u and getattr(u, "is_admin", False))
            finally:
                db.close()
    except Exception:
        pass

    if agent_hash:
        return templates.TemplateResponse(request, "agent_settings_main.html", {"request": request, "agent_hash": agent_hash, "is_admin": is_admin})
    return templates.TemplateResponse(request, "settings.html", {"request": request, "is_admin": is_admin})


# ==================== Agent 路径 fallback 路由 ====================

def _verify_agent_ownership(agent_hash: str, user_id: str) -> bool:
    """验证 Agent 所有权"""
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        return agent is not None and str(agent.user_id) == str(user_id)
    finally:
        db.close()


def _check_agent_configured(agent_hash: str) -> bool:
    """检查 Agent 是否已完成配置页保存"""
    from models.database import SessionLocal, AgentProfile
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        return agent is not None and agent.configured_at is not None
    finally:
        db.close()


def _redirect_to_subdomain(agent_hash: str, path: str = "") -> RedirectResponse:
    """将根域名 agent 路径重定向到子域名

    有 FECLAW_PUBLIC_URL 配置时用子域名，否则返回 None（由调用方决定渲染逻辑）。
    """
    domain = settings.FECLAW_PUBLIC_URL
    if domain:
        return RedirectResponse(url=f"https://{agent_hash}.{domain}{path}", status_code=302)
    return None  # 无域名时由调用方在根域名下渲染


@router.get("/agent/{agent_hash}", response_class=HTMLResponse)
async def agent_dashboard(agent_hash: str, request: Request):
    """Agent 控制台"""
    user_id = await get_user_for_page(request)
    if not user_id:
        return RedirectResponse(url=f"/login?redirect_to=/agent/{agent_hash}", status_code=302)
    if not _verify_agent_ownership(agent_hash, user_id):
        raise HTTPException(status_code=403, detail="无权访问")
    if not _check_agent_configured(agent_hash):
        return RedirectResponse(url=f"/agent/{agent_hash}/configure?reason=unconfigured", status_code=302)
    # 有子域名则重定向，无则渲染主域名下的 Agent 页面
    sub = _redirect_to_subdomain(agent_hash)
    if sub:
        return sub
    return templates.TemplateResponse(request, "agent_dashboard.html", {"request": request, "agent_hash": agent_hash})


@router.get("/agent/{agent_hash}/chat", response_class=HTMLResponse)
async def agent_chat(agent_hash: str, request: Request):
    """Agent 聊天"""
    user_id = await get_user_for_page(request)
    if not user_id:
        return RedirectResponse(url=f"/login?redirect_to=/agent/{agent_hash}/chat", status_code=302)
    if not _verify_agent_ownership(agent_hash, user_id):
        raise HTTPException(status_code=403, detail="无权访问")
    if not _check_agent_configured(agent_hash):
        return RedirectResponse(url=f"/agent/{agent_hash}/configure?reason=unconfigured", status_code=302)
    sub = _redirect_to_subdomain(agent_hash, "/chat")
    if sub:
        return sub
    return templates.TemplateResponse(request, "agent_chat.html", {"request": request, "agent_hash": agent_hash})


@router.get("/agent/{agent_hash}/files", response_class=HTMLResponse)
async def agent_files(agent_hash: str, request: Request):
    """Agent 文件管理"""
    user_id = await get_user_for_page(request)
    if not user_id:
        return RedirectResponse(url=f"/login?redirect_to=/agent/{agent_hash}/files", status_code=302)
    if not _verify_agent_ownership(agent_hash, user_id):
        raise HTTPException(status_code=403, detail="无权访问")
    if not _check_agent_configured(agent_hash):
        return RedirectResponse(url=f"/agent/{agent_hash}/configure?reason=unconfigured", status_code=302)
    sub = _redirect_to_subdomain(agent_hash, "/files")
    if sub:
        return sub
    domain = settings.FECLAW_PUBLIC_URL
    home_url = f"https://{agent_hash}.{domain}" if domain else f"/agent/{agent_hash}"
    return templates.TemplateResponse(request, "agent_files.html", {"request": request, "agent_hash": agent_hash, "home_url": home_url})


@router.get("/agent/{agent_hash}/settings", response_class=HTMLResponse)
async def agent_settings(agent_hash: str, request: Request):
    """Agent 设置 - 重定向到子域名"""
    user_id = await get_user_for_page(request)
    if not user_id:
        return RedirectResponse(url=f"/login?redirect_to=/agent/{agent_hash}/settings", status_code=302)
    if not _verify_agent_ownership(agent_hash, user_id):
        raise HTTPException(status_code=403, detail="无权访问")
    if not _check_agent_configured(agent_hash):
        return RedirectResponse(url=f"/agent/{agent_hash}/configure?reason=unconfigured", status_code=302)
    return _redirect_to_subdomain(agent_hash, "/settings")


@router.get("/agent/{agent_hash}/configure", response_class=HTMLResponse)
async def agent_configure(agent_hash: str, request: Request):
    """Agent 配置页面（新建后配置人格与指令）"""
    user_id = await get_user_for_page(request)
    if not user_id:
        return RedirectResponse(url=f"/login?redirect_to=/agent/{agent_hash}/configure", status_code=302)
    if not _verify_agent_ownership(agent_hash, user_id):
        raise HTTPException(status_code=403, detail="无权访问")
    from urllib.parse import urlparse
    # 有域名用子域名，无域名 fallback 到当前请求 host
    if settings.FECLAW_PUBLIC_URL and settings.FECLAW_SUBDOMAIN_ENABLED:
        agent_base_url = f"https://{agent_hash}.{settings.FECLAW_PUBLIC_URL}"
    else:
        # 从请求中提取 host（兼容 IP:port 和域名）
        host = request.headers.get("host", "localhost:8080")
        agent_base_url = f"http://{host}/agent/{agent_hash}"
    return templates.TemplateResponse(request, "agent_configure.html", {
        "request": request,
        "agent_hash": agent_hash,
        "feclaw_domain": settings.FECLAW_PUBLIC_URL,
        "subdomain_enabled": settings.FECLAW_SUBDOMAIN_ENABLED,
        "agent_base_url": agent_base_url,
    })


# ==================== Agent 初始化 API ====================

class InitializeRequest(BaseModel):
    persona: str
    name: Optional[str] = ""
    description: Optional[str] = ""


@router.get("/api/agent/status")
async def get_agent_status_api(request: Request, user: User = Depends(get_current_user)) -> dict:
    """获取 Agent 状态（JWT 鉴权）"""
    agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=400, detail="Invalid agent hash")

    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        if not agent:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Agent not found")

        if str(agent.user_id) != str(user.id):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="无权访问")

        from services.agent_init_service import agent_init_service
        status = agent_init_service.get_agent_status(agent)

        # 统计用户维度的数据
        agent_count = db.query(func.count(AgentProfile.id)).filter(
            AgentProfile.user_id == agent.user_id
        ).scalar() or 0

        session_count = db.query(func.count(ConversationSession.id)).filter(
            ConversationSession.user_id == agent.user_id,
            ConversationSession.is_archived == False
        ).scalar() or 0

        total_tokens = db.query(func.sum(ConversationSession.token_count)).filter(
            ConversationSession.user_id == agent.user_id
        ).scalar() or 0

        return {
            "agent_hash": agent_hash,
            "name": agent.name,
            "description": agent.description,
            "status": agent.status,
            "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None,
            "profile_files": status.get("profile_files", {}),
            "vfs_directories": status.get("vfs_directories", []),
            "conversations": session_count,
            "tokens": f"{total_tokens:,}",
            "agents": agent_count,
            "storage": "N/A",
        }
    finally:
        db.close()


@router.post("/api/agent/initialize")
async def initialize_agent_api(request: Request, body: InitializeRequest, user: User = Depends(get_current_user)) -> dict:
    """初始化 Agent（JWT 鉴权）"""
    agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=400, detail="Invalid agent hash")

    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        if not agent:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Agent not found")

        if str(agent.user_id) != str(user.id):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="无权访问")

        if agent.status == "initialized":
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Agent already initialized")

        from services.agent_init_service import agent_init_service

        # 更新 Agent 名称和描述
        if body.name:
            agent.name = body.name
        if body.description:
            agent.description = body.description

        # 初始化 Agent（创建 persona、tools.json、config.json、VFS 目录）
        result = agent_init_service.initialize_agent(
            db=db,
            agent=agent,
            persona=body.persona
        )

        return {
            "status": "success",
            "message": f"Agent {agent_hash} initialized successfully",
            "result": result
        }
    finally:
        db.close()


# ==================== 配置管理 API ====================

@router.get("/api/settings")
async def get_settings_api(request: Request, user: User = Depends(get_current_user)) -> dict:
    """获取所有配置（JWT 鉴权）"""
    agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=400, detail="Invalid agent hash")

    # 验证用户所有权
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        if not agent:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Agent not found")
        if str(agent.user_id) != str(user.id):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="无权访问")
        _sr_enabled = agent.sr_enabled
    finally:
        db.close()

    from services.agent_tools_service import AgentToolsService
    service = AgentToolsService(agent_hash)

    # 获取全局配置
    global_config = service.get_effective_config()

    # 获取 Web 渠道配置
    web_config = service.get_effective_config("web")

    # 获取微信渠道配置
    wechat_config = service.get_effective_config("wechat")

    return {
        "global": global_config,
        "web": web_config,
        "wechat": wechat_config,
        "sr_enabled": _sr_enabled
    }


class SettingsUpdateRequest(BaseModel):
    key: str
    value: str
    channel: Optional[str] = None  # None 表示全局配置


@router.put("/api/settings")
async def update_settings_api(request: Request, body: SettingsUpdateRequest, user: User = Depends(get_current_user)) -> dict:
    """更新配置（JWT 鉴权）"""
    agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=400, detail="Invalid agent hash")

    # 验证用户所有权
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        if not agent:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Agent not found")
        if str(agent.user_id) != str(user.id):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="无权访问")

        # sr_enabled 是 AgentProfile 字段，直接更新
        if body.key == "sr_enabled":
            val = (body.value or "").lower() in ("true", "1", "yes")
            agent.sr_enabled = val
            # 同步到 AgentConfig（VFS 配置编辑器也读这个）
            from models.database import AgentConfig
            existing = db.query(AgentConfig).filter(
                AgentConfig.key == f"agents/{agent_hash}/sr_enabled",
                AgentConfig.permission != "none"
            ).first()
            if existing:
                existing.value = body.value
                existing.updated_at = __import__("datetime").datetime.utcnow()
            db.commit()
            return {"status": "success", "message": f"sr_enabled set to {val}"}
    finally:
        db.close()

    from services.agent_tools_service import AgentToolsService
    service = AgentToolsService(agent_hash)

    result = service.set_config(body.key, body.value, body.channel)

    if result.startswith("Error"):
        pass  # HTTPException already imported at module level
        raise HTTPException(status_code=400, detail=result)

    return {"status": "success", "message": result}


@router.get("/filemanager", include_in_schema=False)
@router.get("/filemanager/", include_in_schema=False)
async def filemanager_page(request: Request):
    """Serve filemanager SPA for agent VFS"""
    import os as _os
    html_path = _os.path.join(_os.path.dirname(__file__), "..", "app", "public", "filemanager", "index.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@router.post("/api/file/upload")
async def upload_file_vfs(
    path: str,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user)
):
    """Upload file to agent VFS"""
    import os as _os
    if _os.path.isabs(path) or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    if path == "public" or path.startswith("public/") or path == "config" or path.startswith("config/"):
        raise HTTPException(status_code=403, detail=f"{'公共空间' if path.startswith('public') else '配置目录'}为只读，不允许上传")
    agent_hash = extract_hash_from_host(_get_domain(request))
    if not agent_hash:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")
    content = await file.read()
    full_path = _vfs_path(agent_hash, path)
    s().upload_file(content, full_path)
    return {"status": "success", "path": path, "size": len(content)}

