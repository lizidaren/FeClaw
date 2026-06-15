"""
静态网站托管 API 路由

提供二级域名静态网站空间的管理接口：
- POST /api/static-sites - 创建站点
- GET /api/static-sites - 列出用户的所有站点
- GET /api/static-sites/{site_id} - 获取站点详情
- PUT /api/static-sites/{site_id} - 更新站点配置
- DELETE /api/static-sites/{site_id} - 删除站点
- GET /api/static-sites/{site_id}/files - 列出站点文件
- POST /api/static-sites/{site_id}/files - 上传文件
- GET /api/static-sites/{site_id}/files/{file_path} - 获取文件内容
- DELETE /api/static-sites/{site_id}/files/{file_path} - 删除文件
"""

from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services.static_site_service import StaticSiteService, StaticSite as StaticSiteData
from services.storage_service import get_storage_service
from utils.auth import get_current_user


router = APIRouter(prefix="/api/static-sites", tags=["静态网站托管"])


# ==================== Pydantic 模型 ====================

class CreateSiteRequest(BaseModel):
    """创建站点请求"""
    subdomain: str = Field(..., min_length=1, max_length=63, description="子域名前缀，如 'lizidaren'")
    custom_cname: Optional[str] = Field(None, max_length=256, description="自定义域名 CNAME")


class UpdateSiteRequest(BaseModel):
    """更新站点请求"""
    status: Optional[str] = Field(None, pattern="^(active|suspended)$", description="站点状态")
    custom_cname: Optional[str] = Field(None, max_length=256, description="自定义域名 CNAME")


class SiteResponse(BaseModel):
    """站点响应"""
    id: int
    user_id: int
    subdomain: str
    root_path: str
    status: str
    custom_cname: Optional[str] = None
    public_url: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SiteFileResponse(BaseModel):
    """站点文件响应"""
    path: str
    size: int
    is_dir: bool
    modified_at: Optional[str] = None


class CheckSubdomainResponse(BaseModel):
    """检查子域名可用性响应"""
    subdomain: str
    available: bool


# ==================== 服务实例 ====================

def get_static_site_service() -> StaticSiteService:
    """获取 StaticSiteService 实例"""
    storage = get_storage_service()
    return StaticSiteService(storage)


def site_to_response(site: StaticSiteData) -> SiteResponse:
    """将 StaticSite dataclass 转换为响应模型"""
    service = get_static_site_service()
    return SiteResponse(
        id=site.id,
        user_id=site.user_id,
        subdomain=site.subdomain,
        root_path=site.root_path,
        status=site.status,
        custom_cname=site.custom_cname,
        public_url=service.get_public_url(site),
        created_at=site.created_at.strftime("%Y-%m-%d %H:%M:%S") if site.created_at else None,
        updated_at=site.updated_at.strftime("%Y-%m-%d %H:%M:%S") if site.updated_at else None,
    )


# ==================== 站点管理 API ====================

@router.post("", response_model=SiteResponse, summary="创建站点")
async def create_site(
    request: CreateSiteRequest,
    user_id: int = Depends(get_current_user)
):
    """
    创建新的静态网站站点
    
    - **subdomain**: 子域名前缀，如 'lizidaren'，最终访问地址为 https://lizidaren.site.firstentrance.net
    - **custom_cname**: 可选的自定义域名 CNAME
    """
    service = get_static_site_service()
    
    # 检查子域名是否可用
    if not service.check_subdomain_available(request.subdomain):
        raise HTTPException(status_code=409, detail=f"Subdomain '{request.subdomain}' is already taken")
    
    try:
        site = service.create_site(
            user_id=user_id,
            subdomain=request.subdomain,
            custom_cname=request.custom_cname
        )
        return site_to_response(site)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=List[SiteResponse], summary="列出用户的站点")
async def list_sites(user_id: int = Depends(get_current_user)):
    """列出当前用户的所有静态网站站点"""
    service = get_static_site_service()
    sites = service.list_user_sites(user_id)
    return [site_to_response(s) for s in sites]


@router.get("/check-subdomain", response_model=CheckSubdomainResponse, summary="检查子域名可用性")
async def check_subdomain(
    subdomain: str = Query(..., min_length=1, max_length=63, description="要检查的子域名")
):
    """检查子域名是否可用"""
    service = get_static_site_service()
    available = service.check_subdomain_available(subdomain)
    return CheckSubdomainResponse(subdomain=subdomain, available=available)


@router.get("/{site_id}", response_model=SiteResponse, summary="获取站点详情")
async def get_site(
    site_id: int,
    user_id: int = Depends(get_current_user)
):
    """获取指定站点的详细信息"""
    service = get_static_site_service()
    site = service.get_site(site_id, user_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    return site_to_response(site)


@router.put("/{site_id}", response_model=SiteResponse, summary="更新站点配置")
async def update_site(
    site_id: int,
    request: UpdateSiteRequest,
    user_id: int = Depends(get_current_user)
):
    """
    更新站点配置
    
    - **status**: 站点状态，'active' 或 'suspended'
    - **custom_cname**: 自定义域名 CNAME
    """
    service = get_static_site_service()
    
    # 过滤掉 None 值
    update_data = {k: v for k, v in request.dict().items() if v is not None}
    
    site = service.update_site(site_id, user_id, **update_data)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    return site_to_response(site)


@router.delete("/{site_id}", summary="删除站点")
async def delete_site(
    site_id: int,
    user_id: int = Depends(get_current_user)
) -> dict:
    """删除站点（软删除）"""
    service = get_static_site_service()
    success = service.delete_site(site_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Site not found")
    return {"message": "Site deleted successfully"}


# ==================== 文件管理 API ====================

@router.get("/{site_id}/files", response_model=List[SiteFileResponse], summary="列出站点文件")
async def list_files(
    site_id: int,
    path: str = Query("/", description="目录路径，默认为根目录"),
    user_id: int = Depends(get_current_user)
):
    """列出站点指定目录下的文件和子目录"""
    service = get_static_site_service()
    
    try:
        files = service.list_files(site_id, user_id, path)
        return [
            SiteFileResponse(
                path=f.path,
                size=f.size,
                is_dir=f.is_dir,
                modified_at=f.modified_at.strftime("%Y-%m-%d %H:%M:%S") if f.modified_at else None
            )
            for f in files
        ]
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("/{site_id}/files", summary="上传文件")
async def upload_file(
    site_id: int,
    file: UploadFile = File(..., description="要上传的文件"),
    remote_path: str = Form(..., description="目标路径，如 'css/style.css'"),
    user_id: int = Depends(get_current_user)
) -> dict:
    """
    上传文件到站点
    
    - **file**: 要上传的文件
    - **remote_path**: 目标路径（相对于站点根目录），如 'images/logo.png'
    """
    service = get_static_site_service()
    
    # 读取文件内容
    content = await file.read()
    
    try:
        success = service.upload_file(site_id, user_id, remote_path, content)
        return {"message": "File uploaded successfully", "path": remote_path, "size": len(content)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{site_id}/files/{file_path:path}", summary="获取文件内容")
async def get_file(
    site_id: int,
    file_path: str,
    user_id: int = Depends(get_current_user)
):
    """获取站点文件内容"""
    service = get_static_site_service()
    
    try:
        content = service.get_file_content(site_id, user_id, file_path)
        if content is None:
            raise HTTPException(status_code=404, detail="File not found")
        
        # 尝试检测 Content-Type
        content_type = "application/octet-stream"
        if file_path.endswith(".html"):
            content_type = "text/html; charset=utf-8"
        elif file_path.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif file_path.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        elif file_path.endswith(".json"):
            content_type = "application/json; charset=utf-8"
        elif file_path.endswith(".png"):
            content_type = "image/png"
        elif file_path.endswith(".jpg") or file_path.endswith(".jpeg"):
            content_type = "image/jpeg"
        elif file_path.endswith(".gif"):
            content_type = "image/gif"
        elif file_path.endswith(".svg"):
            content_type = "image/svg+xml"
        
        return Response(content=content, media_type=content_type)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete("/{site_id}/files/{file_path:path}", summary="删除文件")
async def delete_file(
    site_id: int,
    file_path: str,
    user_id: int = Depends(get_current_user)
) -> dict:
    """删除站点文件"""
    service = get_static_site_service()
    
    try:
        success = service.delete_file(site_id, user_id, file_path)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete file")
        return {"message": "File deleted successfully", "path": file_path}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


# ==================== 使用统计 API ====================

class DailyStatsResponse(BaseModel):
    """每日统计响应"""
    date: str
    visit_count: int
    bandwidth_bytes: int
    unique_ips: int
    request_count: int


class UsageStatsResponse(BaseModel):
    """使用统计响应"""
    site_id: int
    total_visits: int
    total_bandwidth_bytes: int
    total_requests: int
    daily_stats: List[DailyStatsResponse]


class PopularPageResponse(BaseModel):
    """热门页面响应"""
    path: str
    visits: int
    bandwidth: int


@router.get("/{site_id}/stats", response_model=UsageStatsResponse, summary="获取站点使用统计")
async def get_site_stats(
    site_id: int,
    days: int = Query(7, ge=1, le=30, description="统计天数，1-30 天"),
    user_id: int = Depends(get_current_user)
):
    """
    获取站点使用统计

    - **days**: 统计天数，默认 7 天，最多 30 天

    返回：
    - total_visits: 总访问量（HTML 页面访问）
    - total_bandwidth_bytes: 总带宽使用（字节）
    - total_requests: 总请求数（包括静态资源）
    - daily_stats: 每日统计详情
    """
    service = get_static_site_service()
    stats = service.get_usage_stats(site_id, user_id, days)

    if not stats:
        raise HTTPException(status_code=404, detail="Site not found or access denied")

    return UsageStatsResponse(
        site_id=stats.site_id,
        total_visits=stats.total_visits,
        total_bandwidth_bytes=stats.total_bandwidth_bytes,
        total_requests=stats.total_requests,
        daily_stats=[
            DailyStatsResponse(
                date=str(s.date),
                visit_count=s.visit_count,
                bandwidth_bytes=s.bandwidth_bytes,
                unique_ips=s.unique_ips,
                request_count=s.request_count
            )
            for s in stats.daily_stats
        ]
    )


@router.get("/{site_id}/stats/popular", response_model=List[PopularPageResponse], summary="获取热门页面")
async def get_popular_pages(
    site_id: int,
    days: int = Query(7, ge=1, le=30, description="统计天数，1-30 天"),
    limit: int = Query(10, ge=1, le=50, description="返回数量，1-50"),
    user_id: int = Depends(get_current_user)
):
    """
    获取热门页面排行

    - **days**: 统计天数，默认 7 天
    - **limit**: 返回数量，默认 10 条
    """
    service = get_static_site_service()
    pages = service.get_popular_pages(site_id, user_id, days, limit)

    return [
        PopularPageResponse(
            path=p["path"],
            visits=p["visits"],
            bandwidth=p["bandwidth"]
        )
        for p in pages
    ]


@router.get("/stats/all", response_model=Dict[int, UsageStatsResponse], summary="获取所有站点统计")
async def get_all_sites_stats(
    days: int = Query(7, ge=1, le=30, description="统计天数，1-30 天"),
    user_id: int = Depends(get_current_user)
):
    """
    获取当前用户所有站点的使用统计

    - **days**: 统计天数，默认 7 天
    """
    service = get_static_site_service()
    all_stats = service.get_all_sites_usage(user_id, days)

    return {
        site_id: UsageStatsResponse(
            site_id=stats.site_id,
            total_visits=stats.total_visits,
            total_bandwidth_bytes=stats.total_bandwidth_bytes,
            total_requests=stats.total_requests,
            daily_stats=[
                DailyStatsResponse(
                    date=str(s.date),
                    visit_count=s.visit_count,
                    bandwidth_bytes=s.bandwidth_bytes,
                    unique_ips=s.unique_ips,
                    request_count=s.request_count
                )
                for s in stats.daily_stats
            ]
        )
        for site_id, stats in all_stats.items()
    }


# ==================== CNAME 验证 API ====================

class CnameVerifyResponse(BaseModel):
    """CNAME 验证响应"""
    verified: bool
    expected_cname: Optional[str] = None
    actual_cname: Optional[str] = None
    error: Optional[str] = None


class CnameStatusResponse(BaseModel):
    """CNAME 状态响应"""
    custom_cname: Optional[str] = None
    verified: bool
    verified_at: Optional[str] = None
    expected_cname: Optional[str] = None


@router.post("/{site_id}/cname/verify", response_model=CnameVerifyResponse, summary="验证 CNAME")
async def verify_cname(
    site_id: int,
    user_id: int = Depends(get_current_user)
):
    """
    验证自定义域名的 CNAME 是否正确指向站点

    验证逻辑：
    1. 检查站点是否设置了 custom_cname
    2. 使用 DNS 查询获取 custom_cname 的 CNAME 记录
    3. 检查 CNAME 是否指向 {subdomain}.site.firstentrance.net
    4. 更新数据库中的验证状态

    返回：
    - verified: 是否验证成功
    - expected_cname: 期望的 CNAME 目标
    - actual_cname: 实际的 CNAME 记录
    - error: 错误信息（如果验证失败）
    """
    service = get_static_site_service()

    try:
        result = service.verify_cname(site_id, user_id)
        return CnameVerifyResponse(
            verified=result["verified"],
            expected_cname=result["expected_cname"],
            actual_cname=result["actual_cname"],
            error=result["error"]
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{site_id}/cname/status", response_model=CnameStatusResponse, summary="获取 CNAME 状态")
async def get_cname_status(
    site_id: int,
    user_id: int = Depends(get_current_user)
):
    """
    获取 CNAME 验证状态

    返回：
    - custom_cname: 用户设置的自定义域名
    - verified: 是否已验证
    - verified_at: 验证时间
    - expected_cname: 期望的 CNAME 目标
    """
    service = get_static_site_service()
    result = service.get_cname_status(site_id, user_id)

    return CnameStatusResponse(
        custom_cname=result["custom_cname"],
        verified=result["verified"],
        verified_at=result["verified_at"].strftime("%Y-%m-%d %H:%M:%S") if result["verified_at"] else None,
        expected_cname=result["expected_cname"]
    )
