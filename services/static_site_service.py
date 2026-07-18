from __future__ import annotations

"""
静态网站托管服务 (Static Site Service)

提供用户二级域名静态网站空间的管理能力。
设计文档: docs/STATIC_SITE_DESIGN.md

Phase 1 实现：
- 站点创建/查询/删除（数据库管理）
- 文件管理（COS 对象存储）

Phase 2 实现：
- 使用统计（访问量、带宽）
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta, timezone

from models.database import SessionLocal
from models.database import StaticSite as StaticSiteModel
from models.database import StaticSiteUsage as StaticSiteUsageModel
from models.database import StaticSiteVisitLog as StaticSiteVisitLogModel

logger = logging.getLogger(__name__)


@dataclass
class StaticSite:
    """静态站点数据模型"""
    id: int
    user_id: int
    subdomain: str          # 如 "lizidaren" → lizidaren.site.firstentrance.net
    root_path: str          # VFS 路径: /workspace/{user_id}/public_html/
    status: str             # active / suspended / deleted
    custom_cname: Optional[str] = None  # 用户自定义域名 CNAME
    cname_verified: bool = False  # CNAME 是否已验证
    cname_verified_at: Optional[datetime] = None  # 验证时间
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class SiteFile:
    """站点文件信息"""
    path: str        # 相对于 root_path 的路径，如 "css/style.css"
    size: int        # 文件大小（字节）
    is_dir: bool
    modified_at: Optional[datetime] = None


@dataclass
class SiteUsageStats:
    """站点使用统计数据"""
    site_id: int
    date: date
    visit_count: int = 0
    bandwidth_bytes: int = 0
    unique_ips: int = 0
    request_count: int = 0


@dataclass
class SiteUsageSummary:
    """站点使用统计汇总"""
    site_id: int
    total_visits: int = 0
    total_bandwidth_bytes: int = 0
    total_requests: int = 0
    daily_stats: List[SiteUsageStats] = field(default_factory=list)


class StaticSiteService:
    """
    静态网站托管服务

    提供站点管理和文件管理功能。
    完整实现待 Phase 1-4 逐步完成。
    """

    # COS 存储路径前缀
    COS_STATIC_SITES_PREFIX = "firstentrance/static-sites/"

    def __init__(self, storage_service, vfs_service=None):
        """
        初始化静态站点服务

        Args:
            storage_service: StorageService 实例（COS 操作）
            vfs_service: VirtualFileSystem 服务（可选，用于 VFS 内文件操作）
        """
        self._storage = storage_service
        self._vfs = vfs_service

    # ==================== 站点管理 ====================

    def create_site(self, user_id: int, subdomain: str, custom_cname: str = None) -> StaticSite:
        """
        创建新站点

        Args:
            user_id: 用户 ID
            subdomain: 子域名前缀（如 "lizidaren"）
            custom_cname: 自定义域名（可选）

        Returns:
            StaticSite 实例

        Raises:
            ValueError: subdomain 格式无效或已被占用
        """
        # 验证 subdomain 格式（a-z, 0-9, -，最多63字符）
        if not self._validate_subdomain(subdomain):
            raise ValueError(f"Invalid subdomain: {subdomain}")

        root_path = f"/workspace/{user_id}/public_html/"

        db = SessionLocal()
        try:
            db_site = StaticSiteModel(
                user_id=str(user_id),
                subdomain=subdomain,
                root_path=root_path,
                status="active",
                custom_cname=custom_cname,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(db_site)
            db.commit()
            db.refresh(db_site)
            return self._db_to_dataclass(db_site)
        finally:
            db.close()

    def get_site(self, site_id: int, user_id: int = None) -> Optional[StaticSite]:
        """
        获取站点详情

        Args:
            site_id: 站点 ID
            user_id: 用户 ID（可选，传入了则验证归属）

        Returns:
            StaticSite 或 None
        """
        db = SessionLocal()
        try:
            query = db.query(StaticSiteModel).filter(StaticSiteModel.id == site_id)
            if user_id is not None:
                query = query.filter(StaticSiteModel.user_id == str(user_id))
            row = query.first()
            if not row:
                return None
            return self._db_to_dataclass(row)
        finally:
            db.close()

    def list_user_sites(self, user_id: int) -> List[StaticSite]:
        """列出用户的所有站点"""
        db = SessionLocal()
        try:
            rows = db.query(StaticSiteModel).filter(
                StaticSiteModel.user_id == str(user_id),
                StaticSiteModel.status != "deleted"
            ).all()
            return [self._db_to_dataclass(r) for r in rows]
        finally:
            db.close()

    def update_site(self, site_id: int, user_id: int, **kwargs) -> Optional[StaticSite]:
        """
        更新站点配置

        Args:
            site_id: 站点 ID
            user_id: 用户 ID（用于权限验证）
            **kwargs: 可更新字段 (status, custom_cname)
        """
        allowed = {"status", "custom_cname"}
        update = {k: v for k, v in kwargs.items() if k in allowed}
        if not update:
            return self.get_site(site_id, user_id)

        update["updated_at"] = datetime.now(timezone.utc)
        db = SessionLocal()
        try:
            row = db.query(StaticSiteModel).filter(
                StaticSiteModel.id == site_id,
                StaticSiteModel.user_id == str(user_id)
            ).first()
            if not row:
                return None
            for k, v in update.items():
                setattr(row, k, v)
            db.commit()
            db.refresh(row)
            return self._db_to_dataclass(row)
        finally:
            db.close()

    def delete_site(self, site_id: int, user_id: int) -> bool:
        """删除站点（软删除，设置 status='deleted'）"""
        db = SessionLocal()
        try:
            row = db.query(StaticSiteModel).filter(
                StaticSiteModel.id == site_id,
                StaticSiteModel.user_id == str(user_id)
            ).first()
            if not row:
                return False
            row.status = "deleted"
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
            return True
        finally:
            db.close()

    # ==================== 文件管理 ====================

    def list_files(self, site_id: int, user_id: int, path: str = "/") -> List[SiteFile]:
        """
        列出站点目录下的文件（COS 对象存储）

        Args:
            site_id: 站点 ID
            user_id: 用户 ID（权限验证）
            path: 相对于 root_path 的路径，默认 "/"

        Returns:
            SiteFile 列表

        Raises:
            PermissionError: 用户无权访问此站点
            ValueError: 站点不存在
        """
        # 获取站点信息
        site = self.get_site(site_id, user_id)
        if not site:
            raise ValueError(f"Site {site_id} not found or access denied")

        # 构建 COS 前缀
        cos_prefix = self.get_cos_key(site, path.lstrip("/"))
        if not cos_prefix.endswith("/"):
            cos_prefix += "/"

        # 从 COS 列出对象
        try:
            objects = self._storage.list_objects(cos_prefix)
            if not objects:
                return []

            files: List[SiteFile] = []
            prefix_len = len(cos_prefix)

            for obj in objects:
                key = obj["Key"]
                rel_path = key[prefix_len:].lstrip("/")

                if not rel_path:
                    continue

                # 提取一级目录/文件
                if "/" in rel_path:
                    # 子目录（取第一级）
                    dir_name = rel_path.split("/")[0]
                    if not any(f.path == dir_name for f in files):
                        files.append(SiteFile(
                            path=dir_name,
                            size=4096,
                            is_dir=True,
                            modified_at=None
                        ))
                else:
                    # 文件
                    files.append(SiteFile(
                        path=rel_path,
                        size=int(obj.get("Size", 0) or 0),
                        is_dir=False,
                        modified_at=self._parse_cos_date(obj.get("LastModified", ""))
                    ))

            return files
        except Exception as e:
            raise RuntimeError(f"Failed to list files: {e}")

    def upload_file(self, site_id: int, user_id: int, remote_path: str, content: bytes) -> bool:
        """
        上传文件到站点（COS 对象存储）

        Args:
            site_id: 站点 ID
            user_id: 用户 ID
            remote_path: 目标路径（如 "css/style.css"）
            content: 文件内容（字节）

        Returns:
            是否成功

        Raises:
            PermissionError: 用户无权访问此站点
            ValueError: 站点不存在或路径无效
        """
        # 获取站点信息
        site = self.get_site(site_id, user_id)
        if not site:
            raise ValueError(f"Site {site_id} not found or access denied")

        # 禁止上传到敏感路径
        if remote_path.startswith("/") or ".." in remote_path:
            raise ValueError(f"Invalid remote_path: {remote_path}")

        # 构建 COS key
        cos_key = self.get_cos_key(site, remote_path.lstrip("/"))

        # 上传到 COS
        try:
            self._storage.put_object(cos_key, content)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to upload file: {e}")

    def delete_file(self, site_id: int, user_id: int, remote_path: str) -> bool:
        """
        删除站点文件（COS 对象存储）

        Args:
            site_id: 站点 ID
            user_id: 用户 ID
            remote_path: 文件路径（如 "css/style.css"）

        Returns:
            是否成功

        Raises:
            PermissionError: 用户无权访问此站点
            ValueError: 站点不存在或路径无效
        """
        # 获取站点信息
        site = self.get_site(site_id, user_id)
        if not site:
            raise ValueError(f"Site {site_id} not found or access denied")

        # 禁止删除敏感路径
        if remote_path.startswith("/") or ".." in remote_path:
            raise ValueError(f"Invalid remote_path: {remote_path}")

        # 构建 COS key
        cos_key = self.get_cos_key(site, remote_path.lstrip("/"))

        # 从 COS 删除
        try:
            # 构建完整 URL 用于删除
            from config import settings
            url = f"https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{cos_key}"
            return self._storage.delete_file(url)
        except Exception as e:
            raise RuntimeError(f"Failed to delete file: {e}")

    def get_file_content(self, site_id: int, user_id: int, remote_path: str) -> Optional[bytes]:
        """
        获取文件内容（COS 对象存储）

        Args:
            site_id: 站点 ID
            user_id: 用户 ID
            remote_path: 文件路径（如 "css/style.css"）

        Returns:
            文件内容（字节），不存在返回 None

        Raises:
            PermissionError: 用户无权访问此站点
            ValueError: 站点不存在或路径无效
        """
        # 获取站点信息
        site = self.get_site(site_id, user_id)
        if not site:
            raise ValueError(f"Site {site_id} not found or access denied")

        # 禁止访问敏感路径
        if remote_path.startswith("/") or ".." in remote_path:
            raise ValueError(f"Invalid remote_path: {remote_path}")

        # 构建 COS key
        cos_key = self.get_cos_key(site, remote_path.lstrip("/"))

        # 从 COS 下载
        try:
            return self._storage.get_file_content(cos_key)
        except Exception as e:
            raise RuntimeError(f"Failed to get file content: {e}")

    # ==================== 公开访问 ====================

    def get_public_url(self, site: StaticSite, file_path: str = "") -> str:
        """
        获取文件的公开访问 URL

        Args:
            site: StaticSite 实例
            file_path: 文件路径（相对于 root_path）

        Returns:
            公开访问 URL（通过 *.site.firstentrance.net 域名）
        """
        if file_path:
            return f"https://{site.subdomain}.site.firstentrance.net/{file_path}"
        return f"https://{site.subdomain}.site.firstentrance.net/"

    def get_cos_key(self, site: StaticSite, file_path: str = "") -> str:
        """
        获取 COS 对象 key

        Args:
            site: StaticSite 实例
            file_path: 文件路径（相对于 root_path）

        Returns:
            COS key，如 "firstentrance/static-sites/123/index.html"
        """
        user_id = str(site.user_id)
        prefix = self.COS_STATIC_SITES_PREFIX
        if file_path:
            return f"{prefix}{user_id}/{file_path}"
        return f"{prefix}{user_id}/"

    # ==================== 使用统计 ====================

    def record_visit(
        self,
        site_id: int,
        file_path: str,
        client_ip: str = None,
        user_agent: str = None,
        referer: str = None,
        response_size: int = 0,
        response_status: int = 200
    ) -> bool:
        """
        记录一次访问

        Args:
            site_id: 站点 ID
            file_path: 访问的文件路径
            client_ip: 客户端 IP
            user_agent: User-Agent
            referer: 来源页面
            response_size: 响应大小（字节）
            response_status: HTTP 状态码

        Returns:
            是否成功
        """
        db = SessionLocal()
        try:
            today = date.today()

            # 1. 记录详细日志
            log = StaticSiteVisitLogModel(
                site_id=site_id,
                file_path=file_path,
                client_ip=client_ip,
                user_agent=user_agent,
                referer=referer,
                response_size=response_size,
                response_status=response_status,
                created_at=datetime.now(timezone.utc)
            )
            db.add(log)

            # 2. 更新每日汇总
            usage = db.query(StaticSiteUsageModel).filter(
                StaticSiteUsageModel.site_id == site_id,
                StaticSiteUsageModel.date == today
            ).first()

            if usage:
                usage.request_count += 1
                usage.bandwidth_bytes += response_size
                # 对于 HTML 页面访问，增加 visit_count
                if file_path.endswith(('.html', '.htm')) or file_path == '' or '/' in file_path and not '.' in file_path.split('/')[-1]:
                    usage.visit_count += 1
                usage.updated_at = datetime.now(timezone.utc)
            else:
                # 创建新的每日记录
                is_page_visit = file_path.endswith(('.html', '.htm')) or file_path == '' or '/' in file_path and not '.' in file_path.split('/')[-1]
                usage = StaticSiteUsageModel(
                    site_id=site_id,
                    date=today,
                    visit_count=1 if is_page_visit else 0,
                    bandwidth_bytes=response_size,
                    request_count=1,
                    unique_ips=1 if client_ip else 0,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc)
                )
                db.add(usage)

            # 3. 异步更新 unique_ips（简化实现：每天第一次访问时计算）
            # 生产环境可用定时任务重新计算
            if client_ip and usage:
                # 检查今天是否已经有这个 IP 的记录
                ip_exists = db.query(StaticSiteVisitLogModel).filter(
                    StaticSiteVisitLogModel.site_id == site_id,
                    StaticSiteVisitLogModel.client_ip == client_ip,
                    StaticSiteVisitLogModel.created_at >= datetime.combine(today, datetime.min.time())
                ).first()
                if not ip_exists:
                    usage.unique_ips += 1

            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logger.error(f"[StaticSiteService] record_visit error: {e}")
            return False
        finally:
            db.close()

    def get_usage_stats(self, site_id: int, user_id: int, days: int = 7) -> Optional[SiteUsageSummary]:
        """
        获取站点使用统计

        Args:
            site_id: 站点 ID
            user_id: 用户 ID（权限验证）
            days: 统计天数，默认 7 天

        Returns:
            SiteUsageSummary 或 None
        """
        # 验证权限
        site = self.get_site(site_id, user_id)
        if not site:
            return None

        db = SessionLocal()
        try:
            today = date.today()
            start_date = today - timedelta(days=days - 1)

            # 查询每日统计
            daily_records = db.query(StaticSiteUsageModel).filter(
                StaticSiteUsageModel.site_id == site_id,
                StaticSiteUsageModel.date >= start_date,
                StaticSiteUsageModel.date <= today
            ).order_by(StaticSiteUsageModel.date.desc()).all()

            # 构建每日统计列表
            daily_stats = []
            total_visits = 0
            total_bandwidth = 0
            total_requests = 0

            for record in daily_records:
                daily_stats.append(SiteUsageStats(
                    site_id=record.site_id,
                    date=record.date,
                    visit_count=record.visit_count,
                    bandwidth_bytes=record.bandwidth_bytes,
                    unique_ips=record.unique_ips,
                    request_count=record.request_count
                ))
                total_visits += record.visit_count
                total_bandwidth += record.bandwidth_bytes
                total_requests += record.request_count

            return SiteUsageSummary(
                site_id=site_id,
                total_visits=total_visits,
                total_bandwidth_bytes=total_bandwidth,
                total_requests=total_requests,
                daily_stats=daily_stats
            )
        finally:
            db.close()

    def get_all_sites_usage(self, user_id: int, days: int = 7) -> Dict[int, SiteUsageSummary]:
        """
        获取用户所有站点的使用统计

        Args:
            user_id: 用户 ID
            days: 统计天数

        Returns:
            {site_id: SiteUsageSummary} 字典
        """
        sites = self.list_user_sites(user_id)
        result = {}
        for site in sites:
            stats = self.get_usage_stats(site.id, user_id, days)
            if stats:
                result[site.id] = stats
        return result

    def get_popular_pages(self, site_id: int, user_id: int, days: int = 7, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取热门页面

        Args:
            site_id: 站点 ID
            user_id: 用户 ID（权限验证）
            days: 统计天数
            limit: 返回数量

        Returns:
            [{path, visits, bandwidth}] 列表
        """
        # 验证权限
        site = self.get_site(site_id, user_id)
        if not site:
            return []

        db = SessionLocal()
        try:
            start_date = datetime.now(timezone.utc) - timedelta(days=days)

            # 使用 SQLAlchemy 查询热门页面
            from sqlalchemy import func

            results = db.query(
                StaticSiteVisitLogModel.file_path,
                func.count(StaticSiteVisitLogModel.id).label('visits'),
                func.sum(StaticSiteVisitLogModel.response_size).label('bandwidth')
            ).filter(
                StaticSiteVisitLogModel.site_id == site_id,
                StaticSiteVisitLogModel.created_at >= start_date
            ).group_by(
                StaticSiteVisitLogModel.file_path
            ).order_by(
                func.count(StaticSiteVisitLogModel.id).desc()
            ).limit(limit).all()

            return [
                {
                    "path": r.file_path,
                    "visits": r.visits,
                    "bandwidth": r.bandwidth or 0
                }
                for r in results
            ]
        finally:
            db.close()

    # ==================== 工具方法 ====================

    def _validate_subdomain(self, subdomain: str) -> bool:
        """
        验证子域名格式

        规则:
        - 长度 1-63
        - 字符: a-z, 0-9, -
        - 不能以 - 开头或结尾

        Returns:
            是否有效
        """
        if not subdomain or len(subdomain) > 63:
            return False
        import re
        if not re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$', subdomain):
            return False
        return True

    def check_subdomain_available(self, subdomain: str) -> bool:
        """检查 subdomain 是否可用"""
        db = SessionLocal()
        try:
            exists = db.query(StaticSiteModel).filter(
                StaticSiteModel.subdomain == subdomain,
                StaticSiteModel.status != "deleted"
            ).first()
            return exists is None
        finally:
            db.close()

    # ==================== CNAME 验证 ====================

    def verify_cname(self, site_id: int, user_id: int) -> Dict[str, Any]:
        """
        验证 CNAME 是否正确指向站点

        Args:
            site_id: 站点 ID
            user_id: 用户 ID（权限验证）

        Returns:
            {
                "verified": bool,
                "expected_cname": str,
                "actual_cname": str | None,
                "error": str | None
            }

        Raises:
            ValueError: 站点不存在或未设置 custom_cname
        """
        import dns.resolver

        # 获取站点信息
        site = self.get_site(site_id, user_id)
        if not site:
            raise ValueError(f"Site {site_id} not found or access denied")

        if not site.custom_cname:
            return {
                "verified": False,
                "expected_cname": None,
                "actual_cname": None,
                "error": "No custom CNAME configured"
            }

        # 期望的 CNAME 目标
        expected_cname = f"{site.subdomain}.site.firstentrance.net"

        try:
            # 查询 CNAME 记录
            answers = dns.resolver.resolve(site.custom_cname, 'CNAME')
            actual_cname = str(answers[0]).rstrip('.') if answers else None

            # 检查是否匹配
            verified = actual_cname == expected_cname

            # 更新数据库
            db = SessionLocal()
            try:
                row = db.query(StaticSiteModel).filter(
                    StaticSiteModel.id == site_id,
                    StaticSiteModel.user_id == str(user_id)
                ).first()
                if row:
                    row.cname_verified = verified
                    row.cname_verified_at = datetime.now(timezone.utc) if verified else None
                    row.updated_at = datetime.now(timezone.utc)
                    db.commit()
            finally:
                db.close()

            return {
                "verified": verified,
                "expected_cname": expected_cname,
                "actual_cname": actual_cname,
                "error": None if verified else f"CNAME mismatch: expected {expected_cname}, got {actual_cname}"
            }
        except dns.resolver.NoAnswer:
            # 没有 CNAME 记录
            return {
                "verified": False,
                "expected_cname": expected_cname,
                "actual_cname": None,
                "error": "No CNAME record found"
            }
        except dns.resolver.NXDOMAIN:
            # 域名不存在
            return {
                "verified": False,
                "expected_cname": expected_cname,
                "actual_cname": None,
                "error": f"Domain {site.custom_cname} does not exist"
            }
        except Exception as e:
            # 其他 DNS 错误
            return {
                "verified": False,
                "expected_cname": expected_cname,
                "actual_cname": None,
                "error": f"DNS lookup failed: {str(e)}"
            }

    def get_cname_status(self, site_id: int, user_id: int) -> Dict[str, Any]:
        """
        获取 CNAME 验证状态

        Args:
            site_id: 站点 ID
            user_id: 用户 ID（权限验证）

        Returns:
            {
                "custom_cname": str | None,
                "verified": bool,
                "verified_at": datetime | None,
                "expected_cname": str | None
            }
        """
        site = self.get_site(site_id, user_id)
        if not site:
            return {
                "custom_cname": None,
                "verified": False,
                "verified_at": None,
                "expected_cname": None
            }

        return {
            "custom_cname": site.custom_cname,
            "verified": site.cname_verified,
            "verified_at": site.cname_verified_at,
            "expected_cname": f"{site.subdomain}.site.firstentrance.net" if site.custom_cname else None
        }

    @staticmethod
    def _parse_cos_date(date_str: str) -> Optional[datetime]:
        """解析 COS LastModified 时间字符串"""
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            try:
                return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                return None

    def _db_to_dataclass(self, row: "StaticSiteModel") -> "StaticSite":
        """将数据库行转换为 dataclass 静态站点"""
        return StaticSite(
            id=row.id,
            user_id=int(row.user_id),
            subdomain=row.subdomain,
            root_path=row.root_path,
            status=row.status,
            custom_cname=row.custom_cname,
            cname_verified=row.cname_verified if hasattr(row, 'cname_verified') else False,
            cname_verified_at=row.cname_verified_at if hasattr(row, 'cname_verified_at') else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )