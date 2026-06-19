"""
权限服务单元测试

测试覆盖：
1. Permission 类静态方法 (is_valid, has_read, has_write, LEVELS)
2. PermissionService.__init__ (user_id, agent_hash, db)
3. PermissionService 属性 (user_id, agent_hash, db)
4. PermissionService.close()
5. PermissionService.get_default_permission() (public, sensitive, normal)
6. PermissionService.check_permission() (record exists, no record, various perms)
7. PermissionService.grant_permission() (invalid, create, update)
8. PermissionService.revoke_permission() (exists, not exists)
9. PermissionService.list_permissions() (agent_hash, user_id)
10. PermissionService.get_permission() (record, default)
11. 便捷函数 (check_permission, grant_permission, revoke_permission)

所有测试 mock 外部数据库依赖。
"""

import pytest
from unittest.mock import MagicMock, patch

from services.permission_service import (
    Permission,
    PermissionService,
    check_permission,
    grant_permission,
    revoke_permission,
)
from models.database import FilePermission

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────

_UNSET = object()


def _setup_query_chain(mock_db, first_result=_UNSET, all_result=_UNSET, delete_result=_UNSET):
    """设置 mock_db 的 query 链，让 .filter().filter().first() 等模式工作。

    使用 _UNSET 哨兵值区分"未传参"和"传了 None"，因为很多测试需要 first() 返回 None。
    """
    chain = MagicMock()
    mock_db.query.return_value = chain
    chain.filter.return_value = chain
    chain.order_by.return_value = chain
    if first_result is not _UNSET:
        chain.first.return_value = first_result
    if all_result is not _UNSET:
        chain.all.return_value = all_result
    if delete_result is not _UNSET:
        chain.delete.return_value = delete_result
    return chain


# ─────────────────────────────────────────────
# 1. Permission 类静态方法
# ─────────────────────────────────────────────

class TestPermission:
    """Permission 类静态方法测试"""

    def test_is_valid_true_for_all_defined_permissions(self):
        """is_valid 应对所有已定义的权限返回 True"""
        assert Permission.is_valid("read") is True
        assert Permission.is_valid("write") is True
        assert Permission.is_valid("readwrite") is True
        assert Permission.is_valid("none") is True

    def test_is_valid_false_for_unknown_strings(self):
        """is_valid 应对未定义的权限返回 False"""
        assert Permission.is_valid("invalid") is False
        assert Permission.is_valid("") is False
        assert Permission.is_valid("READ") is False

    def test_has_read_returns_true_for_read_and_readwrite(self):
        """has_read 应对 read 和 readwrite 返回 True"""
        assert Permission.has_read("read") is True
        assert Permission.has_read("readwrite") is True

    def test_has_read_returns_false_for_write_and_none(self):
        """has_read 应对 write 和 none 返回 False"""
        assert Permission.has_read("write") is False
        assert Permission.has_read("none") is False

    def test_has_write_returns_true_for_write_and_readwrite(self):
        """has_write 应对 write 和 readwrite 返回 True"""
        assert Permission.has_write("write") is True
        assert Permission.has_write("readwrite") is True

    def test_has_write_returns_false_for_read_and_none(self):
        """has_write 应对 read 和 none 返回 False"""
        assert Permission.has_write("read") is False
        assert Permission.has_write("none") is False

    def test_levels_mapping(self):
        """LEVELS 应有正确的权限层级"""
        assert Permission.LEVELS == {
            "none": 0,
            "write": 1,
            "read": 2,
            "readwrite": 3,
        }


# ─────────────────────────────────────────────
# 2. PermissionService.__init__
# ─────────────────────────────────────────────

class TestPermissionServiceInit:
    """PermissionService.__init__ 测试"""

    def test_init_with_user_id(self, mock_db):
        """仅传 user_id 时应正确设置内部状态"""
        svc = PermissionService(user_id="user_1", db=mock_db)
        assert svc._user_id == "user_1"
        assert svc._agent_hash is None
        assert svc._db is mock_db

    def test_init_with_agent_hash_finds_user_id(self, mock_db):
        """传 agent_hash 且 Agent 存在时，应从 DB 获取 user_id"""
        mock_agent = MagicMock()
        mock_agent.user_id = 42
        _setup_query_chain(mock_db, first_result=mock_agent)

        svc = PermissionService(agent_hash="abcd", db=mock_db)
        assert svc._user_id == "42"
        assert svc._agent_hash == "abcd"

    def test_init_with_agent_hash_not_found(self, mock_db):
        """传 agent_hash 但 Agent 不存在时，user_id 保持 None"""
        _setup_query_chain(mock_db, first_result=None)

        svc = PermissionService(agent_hash="dead", db=mock_db)
        assert svc._user_id is None

    def test_init_prefers_explicit_user_id_over_agent_hash(self, mock_db):
        """同时传 user_id 和 agent_hash 时，应使用 user_id，不查 AgentProfile"""
        svc = PermissionService(user_id="explicit_user", agent_hash="abcd", db=mock_db)
        assert svc._user_id == "explicit_user"
        # 不应查询数据库找 AgentProfile
        mock_db.query.assert_not_called()

    def test_init_without_db_creates_session(self, mock_db):
        """不传 db 且不传 agent_hash 时，不创建临时 session"""
        svc = PermissionService(user_id="user_1")
        assert svc._db is None
        assert svc._user_id == "user_1"

    def test_init_with_agent_hash_without_db_creates_and_closes_session(self, mock_db):
        """传 agent_hash 且不传 db 时，创建临时 session 并关闭"""
        mock_agent = MagicMock()
        mock_agent.user_id = 7
        _setup_query_chain(mock_db, first_result=mock_agent)

        svc = PermissionService(agent_hash="abcd")
        assert svc._user_id == "7"
        assert svc._db is None  # 临时 session 已关闭
        # 临时 session 应被 close
        mock_db.close.assert_called_once()


# ─────────────────────────────────────────────
# 3. PermissionService 属性
# ─────────────────────────────────────────────

class TestPermissionServiceProperties:
    """PermissionService 属性测试"""

    def test_user_id_when_set(self, mock_db):
        """user_id 属性应返回字符串形式的 user_id"""
        svc = PermissionService(user_id="123", db=mock_db)
        assert svc.user_id == "123"

    def test_user_id_when_none(self, mock_db):
        """user_id 为 None 时应返回空字符串"""
        svc = PermissionService(db=mock_db)
        assert svc.user_id == ""

    def test_agent_hash_when_set(self, mock_db):
        """agent_hash 属性应返回值"""
        svc = PermissionService(agent_hash="abcd", db=mock_db)
        assert svc.agent_hash == "abcd"

    def test_agent_hash_when_none(self, mock_db):
        """agent_hash 为 None 时应返回空字符串"""
        svc = PermissionService(db=mock_db)
        assert svc.agent_hash == ""

    def test_db_lazy_init_creates_session(self, mock_db):
        """db 属性在 _db 为 None 时应创建新 session"""
        svc = PermissionService(user_id="u1")
        assert svc._db is None

        result = svc.db
        assert result is mock_db
        assert svc._db is mock_db

    def test_db_returns_existing_session(self, mock_db):
        """db 属性在 _db 已设置时应直接返回"""
        svc = PermissionService(user_id="u1", db=mock_db)
        result = svc.db
        assert result is mock_db


# ─────────────────────────────────────────────
# 4. PermissionService.close()
# ─────────────────────────────────────────────

class TestPermissionServiceClose:
    """PermissionService.close() 测试"""

    def test_close_with_session(self, mock_db):
        """close 应关闭数据库会话并置 _db 为 None"""
        svc = PermissionService(user_id="u1", db=mock_db)
        svc.close()
        mock_db.close.assert_called_once()
        assert svc._db is None

    def test_close_without_session_is_noop(self):
        """close 在 _db 为 None 时应为无操作"""
        svc = PermissionService(user_id="u1")
        svc.close()  # 不应抛出异常
        assert svc._db is None


# ─────────────────────────────────────────────
# 5. PermissionService.get_default_permission()
# ─────────────────────────────────────────────

class TestPermissionServiceGetDefaultPermission:
    """get_default_permission() 测试"""

    @pytest.fixture
    def svc(self, mock_db):
        return PermissionService(user_id="u1", db=mock_db)

    def test_public_root_returns_read(self, svc):
        """路径 /public 应返回 read"""
        assert svc.get_default_permission("/public") == Permission.READ
        assert svc.get_default_permission("public") == Permission.READ

    def test_public_subpath_returns_read(self, svc):
        """路径 /public/xxx 应返回 read"""
        assert svc.get_default_permission("public/readme.md") == Permission.READ
        assert svc.get_default_permission("/public/readme.md") == Permission.READ

    @pytest.mark.parametrize("file_path", [
        ".env",
        "/.env",
        "test/.env",
        "/test/.env",
        "IDENTITY.md",
        "test/IDENTITY.md",
        "a/b/IDENTITY.md",
        "SOUL.md",
        "sub/SOUL.md",
        "x/y/SOUL.md",
        "SECRET_KEY",
        "subdir/SECRET_KEY",
        "PASSWORD",
        "subdir/PASSWORD",
    ])
    def test_sensitive_files_return_read(self, svc, file_path):
        """敏感文件（.env, IDENTITY.md, SOUL.md, SECRET*, PASSWORD*）应返回 read"""
        assert svc.get_default_permission(file_path) == Permission.READ

    def test_normal_file_returns_readwrite(self, svc):
        """普通文件应返回 readwrite"""
        assert svc.get_default_permission("workspace/main.py") == Permission.READWRITE

    def test_empty_path_returns_readwrite(self, svc):
        """空路径应返回 readwrite"""
        assert svc.get_default_permission("") == Permission.READWRITE
        assert svc.get_default_permission("/") == Permission.READWRITE


# ─────────────────────────────────────────────
# 6. PermissionService.check_permission()
# ─────────────────────────────────────────────

class TestPermissionServiceCheckPermission:
    """check_permission() 测试"""

    def test_record_exists_agent_hash_mode_has_readwrite_require_read(self, mock_db):
        """有权限记录且为 readwrite，要求 read 时应通过"""
        mock_perm = MagicMock()
        mock_perm.permission = "readwrite"
        _setup_query_chain(mock_db, first_result=mock_perm)

        svc = PermissionService(agent_hash="abcd", db=mock_db)
        assert svc.check_permission("workspace/main.py", "read") is True

    def test_record_exists_agent_hash_mode_has_read_require_write(self, mock_db):
        """有权限记录且为 read，要求 write 时应拒绝"""
        mock_perm = MagicMock()
        mock_perm.permission = "read"
        _setup_query_chain(mock_db, first_result=mock_perm)

        svc = PermissionService(agent_hash="abcd", db=mock_db)
        assert svc.check_permission("workspace/main.py", "write") is False

    def test_record_exists_user_id_mode_has_write_require_write(self, mock_db):
        """user_id 模式下有 write 权限记录，要求 write 应通过"""
        mock_perm = MagicMock()
        mock_perm.permission = "write"
        _setup_query_chain(mock_db, first_result=mock_perm)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.check_permission("workspace/main.py", "write") is True

    def test_no_record_normal_file_require_read(self, mock_db):
        """无权限记录，普通文件默认 readwrite，要求 read 应通过"""
        _setup_query_chain(mock_db, first_result=None)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.check_permission("normal.py", "read") is True

    def test_no_record_public_file_require_write(self, mock_db):
        """无权限记录，/public 文件默认 read，要求 write 应拒绝"""
        _setup_query_chain(mock_db, first_result=None)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.check_permission("public/index.html", "write") is False

    def test_no_record_sensitive_file_require_write(self, mock_db):
        """无权限记录，敏感文件默认 read，要求 write 应拒绝"""
        _setup_query_chain(mock_db, first_result=None)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.check_permission(".env", "write") is False

    def test_require_invalid_permission(self, mock_db):
        """required_permission 无效时应返回 False"""
        mock_perm = MagicMock()
        mock_perm.permission = "readwrite"
        _setup_query_chain(mock_db, first_result=mock_perm)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.check_permission("main.py", "invalid") is False

    def test_record_has_none_require_read(self, mock_db):
        """权限为 none 时，要求 read 应拒绝"""
        mock_perm = MagicMock()
        mock_perm.permission = "none"
        _setup_query_chain(mock_db, first_result=mock_perm)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.check_permission("main.py", "read") is False

    def test_path_is_normalized(self, mock_db):
        """路径应以 / 开头被 strip"""
        mock_perm = MagicMock()
        mock_perm.permission = "readwrite"
        chain = _setup_query_chain(mock_db, first_result=mock_perm)

        svc = PermissionService(user_id="u1", db=mock_db)
        svc.check_permission("/workspace/main.py", "read")

        # 验证最后一次 filter 传入的是标准化后的路径
        chain.filter.assert_called()
        last_call = chain.filter.call_args_list[-1]
        assert last_call[0][0].right.value == "workspace/main.py"


# ─────────────────────────────────────────────
# 7. PermissionService.grant_permission()
# ─────────────────────────────────────────────

class TestPermissionServiceGrantPermission:
    """grant_permission() 测试"""

    def test_invalid_permission_returns_false(self, mock_db):
        """无效的权限字符串应返回 False"""
        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.grant_permission("main.py", "invalid") is False
        # 不应 commit
        mock_db.commit.assert_not_called()

    def test_create_new_permission(self, mock_db):
        """无现有记录时应创建新的 FilePermission"""
        _setup_query_chain(mock_db, first_result=None)

        svc = PermissionService(user_id="u1", agent_hash="abcd", db=mock_db)
        assert svc.grant_permission("workspace/main.py", "readwrite") is True

        # 验证 db.add 被调用
        mock_db.add.assert_called_once()
        added = mock_db.add.call_args[0][0]
        assert isinstance(added, FilePermission)
        assert added.user_id == "u1"
        assert added.agent_hash == "abcd"
        assert added.file_path == "workspace/main.py"
        assert added.permission == "readwrite"

        mock_db.commit.assert_called_once()

    def test_update_existing_permission(self, mock_db):
        """有现有记录时应更新权限"""
        existing = MagicMock()
        existing.permission = "read"
        _setup_query_chain(mock_db, first_result=existing)

        svc = PermissionService(user_id="u1", agent_hash="abcd", db=mock_db)
        assert svc.grant_permission("workspace/main.py", "readwrite") is True

        # 更新现有记录
        assert existing.permission == "readwrite"
        assert existing.updated_at is not None
        # 不应调用 add
        mock_db.add.assert_not_called()
        mock_db.commit.assert_called_once()

    def test_grant_permission_none_is_valid(self, mock_db):
        """权限 'none' 是有效的权限值"""
        _setup_query_chain(mock_db, first_result=None)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.grant_permission("main.py", "none") is True

        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()


# ─────────────────────────────────────────────
# 8. PermissionService.revoke_permission()
# ─────────────────────────────────────────────

class TestPermissionServiceRevokePermission:
    """revoke_permission() 测试"""

    def test_delete_existing_permission(self, mock_db):
        """删除存在的权限记录应返回 True"""
        _setup_query_chain(mock_db, delete_result=1)

        svc = PermissionService(user_id="u1", agent_hash="abcd", db=mock_db)
        assert svc.revoke_permission("workspace/main.py") is True
        mock_db.commit.assert_called_once()

    def test_delete_nonexistent_permission(self, mock_db):
        """删除不存在的权限记录应返回 False"""
        _setup_query_chain(mock_db, delete_result=0)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.revoke_permission("nonexistent.py") is False
        mock_db.commit.assert_called_once()


# ─────────────────────────────────────────────
# 9. PermissionService.list_permissions()
# ─────────────────────────────────────────────

class TestPermissionServiceListPermissions:
    """list_permissions() 测试"""

    def test_list_with_agent_hash(self, mock_db):
        """agent_hash 模式下列出该 Agent 的所有权限"""
        perms = [MagicMock(), MagicMock()]
        _setup_query_chain(mock_db, all_result=perms)

        svc = PermissionService(agent_hash="abcd", db=mock_db)
        result = svc.list_permissions()
        assert result == perms

    def test_list_with_user_id(self, mock_db):
        """user_id 模式下列出该用户的所有权限"""
        perms = [MagicMock()]
        _setup_query_chain(mock_db, all_result=perms)

        svc = PermissionService(user_id="u1", db=mock_db)
        result = svc.list_permissions()
        assert result == perms

    def test_list_empty(self, mock_db):
        """无权限时应返回空列表"""
        _setup_query_chain(mock_db, all_result=[])

        svc = PermissionService(user_id="u1", db=mock_db)
        result = svc.list_permissions()
        assert result == []


# ─────────────────────────────────────────────
# 10. PermissionService.get_permission()
# ─────────────────────────────────────────────

class TestPermissionServiceGetPermission:
    """get_permission() 测试"""

    def test_with_record(self, mock_db):
        """有权限记录时应返回记录中的权限"""
        mock_perm = MagicMock()
        mock_perm.permission = "read"
        _setup_query_chain(mock_db, first_result=mock_perm)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.get_permission("main.py") == "read"

    def test_without_record_returns_default(self, mock_db):
        """无权限记录时应返回默认权限"""
        _setup_query_chain(mock_db, first_result=None)

        svc = PermissionService(user_id="u1", db=mock_db)
        # 普通文件默认 readwrite
        assert svc.get_permission("normal.py") == Permission.READWRITE

    def test_without_record_public_returns_read(self, mock_db):
        """无权限记录且为 /public 路径时应返回 read"""
        _setup_query_chain(mock_db, first_result=None)

        svc = PermissionService(user_id="u1", db=mock_db)
        assert svc.get_permission("public/info.md") == Permission.READ


# ─────────────────────────────────────────────
# 11. 便捷函数
# ─────────────────────────────────────────────

class TestConvenienceFunctions:
    """模块级便捷函数测试"""

    def test_check_permission_with_db_provided(self, mock_db):
        """check_permission() 传入 db 时，不应关闭 session"""
        mock_perm = MagicMock()
        mock_perm.permission = "readwrite"
        _setup_query_chain(mock_db, first_result=mock_perm)

        result = check_permission("u1", "main.py", "read", db=mock_db)
        assert result is True
        mock_db.close.assert_not_called()

    def test_check_permission_without_db(self, mock_db):
        """check_permission() 不传 db 时，应自动关闭 session"""
        mock_perm = MagicMock()
        mock_perm.permission = "readwrite"
        _setup_query_chain(mock_db, first_result=mock_perm)

        result = check_permission("u1", "main.py", "read")
        assert result is True
        mock_db.close.assert_called_once()

    def test_grant_permission_with_db_provided(self, mock_db):
        """grant_permission() 传入 db 时，不应关闭 session"""
        _setup_query_chain(mock_db, first_result=None)

        result = grant_permission("u1", "main.py", "read", db=mock_db)
        assert result is True
        mock_db.close.assert_not_called()

    def test_grant_permission_without_db(self, mock_db):
        """grant_permission() 不传 db 时，应自动关闭 session"""
        _setup_query_chain(mock_db, first_result=None)

        result = grant_permission("u1", "main.py", "read")
        assert result is True
        mock_db.close.assert_called_once()

    def test_revoke_permission_with_db_provided(self, mock_db):
        """revoke_permission() 传入 db 时，不应关闭 session"""
        _setup_query_chain(mock_db, delete_result=1)

        result = revoke_permission("u1", "main.py", db=mock_db)
        assert result is True
        mock_db.close.assert_not_called()

    def test_revoke_permission_without_db(self, mock_db):
        """revoke_permission() 不传 db 时，应自动关闭 session"""
        _setup_query_chain(mock_db, delete_result=0)

        result = revoke_permission("u1", "main.py")
        assert result is False
        mock_db.close.assert_called_once()
