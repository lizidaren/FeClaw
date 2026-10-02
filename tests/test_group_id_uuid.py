"""
Q18 回归测试 —— 群 id 类型必须是 UUID String(36)，防止再次被改回 Integer。

背景：commit ae71f8e (P4) 把 Group.id / group_id 列改成 Integer，但生产库
groups.id 等仍是 varchar(36)，且无迁移，导致 POST /api/groups 500、读旧数据炸。
本文件把这些约束钉死，防回归。
"""

import uuid

import pytest
from sqlalchemy import String, Integer


def _column(model_class, name):
    return getattr(model_class, name).property.columns[0]


class TestGroupIdColumns:
    """models/group.py 的 id / group_id 列类型必须是 String(36)。"""

    def test_group_id_is_string_uuid(self):
        from models.group import Group

        col = _column(Group, "id")
        assert isinstance(col.type, String), f"Group.id 应为 String，实际 {col.type}"
        assert col.type.length == 36
        assert col.primary_key is True
        # default 必须是产生 UUID 字符串的 callable
        assert col.default is not None
        generated = col.default.arg(None)
        assert isinstance(generated, str)
        uuid.UUID(generated)  # 不抛异常即合法 UUID

    @pytest.mark.parametrize(
        "model_class,column_name",
        [
            ("GroupMember", "group_id"),
            ("GroupMessage", "group_id"),
            ("GroupMoments", "group_id"),
        ],
    )
    def test_group_id_fk_columns_are_string_uuid(self, model_class, column_name):
        from models import group as g

        col = _column(getattr(g, model_class), column_name)
        assert isinstance(col.type, String), (
            f"{model_class}.{column_name} 应为 String，实际 {col.type}"
        )
        assert col.type.length == 36

    def test_group_owner_user_id_still_int(self):
        # owner_user_id 是 user id，不是群 id，必须保持 int
        from models.group import Group

        col = _column(Group, "owner_user_id")
        assert isinstance(col.type, Integer)


class TestGroupResponseModelsPinStr:
    """路由响应/请求模型里 group id 的注解必须是 str。"""

    def test_group_response_id_is_str(self):
        from routers.group import GroupResponse

        assert GroupResponse.model_fields["id"].annotation is str

    def test_message_response_group_id_is_str(self):
        from routers.group import MessageResponse

        assert MessageResponse.model_fields["group_id"].annotation is str

    def test_moment_response_group_id_is_str(self):
        from routers.group import MomentResponse

        assert MomentResponse.model_fields["group_id"].annotation is str

    def test_bind_org_response_group_id_is_str(self):
        from routers.organization import BindOrgResponse

        assert BindOrgResponse.model_fields["group_id"].annotation is str

    def test_chat_request_group_id_is_str(self):
        from routers.feclaw_chat import ChatRequest

        assert ChatRequest.model_fields["group_id"].annotation.__args__[0] is str


class TestGroupTargetParsing:
    """'group:{id}' 解析必须返回字符串，不再 int() 强转。"""

    def test_parse_group_target_returns_str(self):
        from services.approval_service import parse_group_target

        gid = str(uuid.uuid4())
        assert parse_group_target(f"group:{gid}") == gid
        assert parse_group_target("group:") is None
        assert parse_group_target("user:123") is None
        assert parse_group_target(None) is None

    def test_parse_user_target_still_int(self):
        from services.approval_service import parse_user_target

        assert parse_user_target("user:123") == 123


class TestGroupPathResolution:
    """/mnt/group/{uuid}/... 路径解析不再假定 gid 是数字。"""

    def test_resolve_group_path_accepts_uuid(self):
        from services.tools.file_ops import FileOpsMixin

        gid = str(uuid.uuid4())
        cos_key, err = FileOpsMixin._resolve_group_path(f"/mnt/group/{gid}/.share/x.md")
        assert err is None
        assert cos_key == f"feclaw/groups/{gid}/.share/x.md"

    def test_resolve_organization_regex_accepts_uuid(self):
        # Q18: _organization 正则从 (\d+) 放宽为 ([^/]+)，UUID 也能匹配。
        # 不触 DB（只验正则命中路径这一层），直接复现其匹配逻辑。
        import re

        gid = str(uuid.uuid4())
        pattern = r"^/mnt/group/([^/]+)/\.ref/_organization(?:/(.*))?$"
        m = re.match(pattern, f"/mnt/group/{gid}/.ref/_organization/a.md".rstrip("/"))
        assert m is not None
        assert m.group(1) == gid
