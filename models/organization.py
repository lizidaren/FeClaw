"""
Organization Model — Phase 4

"一切皆群"原则不变：组织是特殊的"父群"，它的 .ref/ 可被子群共享。

绑定关系：
- 一个 Organization 可被多个 Group 绑定
- 一个 Group 最多绑定一个 Organization
- 绑定后，子群的 /mnt/group/{id}/.ref/_organization/ 会显示该组织的 .ref/
"""

from sqlalchemy import Column, String, Boolean, DateTime, Integer, Index
from models.database import Base
from datetime import datetime


class Organization(Base):
    """组织 —— 跨群共享 .ref/ 知识库的载体"""
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), default="")
    owner_user_id = Column(Integer, nullable=False, index=True)
    # 组织级 .ref/ 写入权限：
    # - "owner_only"（默认）：仅创建者可写
    # - "members"：未来扩展，组织内成员都能写
    write_policy = Column(String(20), default="owner_only")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_organizations_owner_user_id", "owner_user_id"),
    )


# Group 表加 organization_id 外键（在 models/group.py 中加，迁移在 main.py lifespan 中跑）
# Group.organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)


__all__ = ["Organization"]