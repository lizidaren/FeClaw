"""工具日志只读路径、清理保留期和手动清理工具测试。"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from services.local_storage import LocalStorage
from services.permission_service import Permission, PermissionService
from services.tool_log_service import (
    cleanup_tool_logs,
    is_tool_log_cos_key,
    is_tool_log_vfs_path,
)
from services.virtual_filesystem import VirtualFileSystem


def test_tool_log_path_detection_accepts_canonical_and_workspace_alias():
    assert is_tool_log_vfs_path(".logs/tools/2026-07-19/bash/a.log")
    assert is_tool_log_vfs_path("/workspace/.logs/tools/2026-07-19/bash/a.log")
    assert is_tool_log_cos_key(
        "feclaw/agents/abcd/.logs/tools/2026-07-19/bash/a.log"
    )
    assert not is_tool_log_vfs_path("workspace/notes.log")


def test_permission_service_makes_logs_non_overridable_read_only():
    service = PermissionService.__new__(PermissionService)
    service._user_id = None
    service._agent_hash = "abcd"
    service._db = MagicMock()

    path = ".logs/tools/2026-07-19/bash/a.log"
    assert service.get_default_permission(path) == Permission.READ
    assert service.check_permission(path, Permission.WRITE) is False
    assert service.grant_permission(path, Permission.READWRITE) is False
    service._db.query.assert_not_called()


def test_vfs_internal_write_is_readable_but_agent_mutations_are_blocked(tmp_path):
    storage = LocalStorage(
        root_dir=str(tmp_path / "storage"),
        public_root=str(tmp_path / "public"),
    )
    vfs = VirtualFileSystem(
        user_id="1",
        agent_id="abcd",
        storage=storage,
    )
    path = ".logs/tools/2026-07-19/bash/call-1.log"

    assert vfs.write_tool_log(path, "完整结果").startswith("OK")
    assert vfs.cat(path) == "完整结果"
    assert vfs.cat(f"/workspace/{path}") == "完整结果"
    assert ".logs" not in vfs.ls("/")
    assert ".logs" not in vfs.ls("/workspace")

    assert "read-only" in vfs.echo("篡改", path)
    assert "read-only" in vfs.echo("追加", f"/workspace/{path}", append=True)
    assert "read-only" in vfs.touch(path)
    assert "read-only" in vfs.mkdir(".logs/new-dir", parents=True)
    assert "read-only" in vfs.rm(path)
    assert "read-only" in vfs.mv(path, "/workspace/moved.log")
    assert "read-only" in vfs.cp("/workspace/source.txt", path)


def test_cleanup_tool_logs_deletes_only_dates_older_than_retention():
    storage = MagicMock()
    prefix = "feclaw/agents/abcd/.logs/tools/"
    storage.list_objects.return_value = [
        {"Key": f"{prefix}2026-07-11/bash/old.log"},
        {"Key": f"{prefix}2026-07-12/bash/boundary.log"},
        {"Key": f"{prefix}2026-07-19/bash/today.log"},
        {"Key": f"{prefix}not-a-date/bash/unknown.log"},
    ]
    storage.delete_file_by_key.return_value = True

    result = cleanup_tool_logs(
        agent_hash="abcd",
        retention_days=7,
        storage=storage,
        now=datetime(2026, 7, 19, 12, 0, 0),
    )

    storage.delete_file_by_key.assert_called_once_with(
        f"{prefix}2026-07-11/bash/old.log"
    )
    assert result == {
        "scanned": 4,
        "deleted": 1,
        "failed": 0,
        "skipped": 3,
        "cutoff_date": "2026-07-12",
        "retention_days": 7,
    }


def test_manual_cleanup_tool_is_scoped_to_current_agent():
    from services.agent_tools_service import AgentToolsService

    with patch("services.tools.base.VirtualFileSystem"), patch(
        "services.tools.base.PermissionService"
    ):
        service = AgentToolsService(agent_hash="abcd")
    service._vfs = MagicMock()

    with patch(
        "services.tools.base.cleanup_tool_logs",
        return_value={
            "scanned": 3,
            "deleted": 2,
            "failed": 0,
            "skipped": 1,
            "cutoff_date": "2026-07-12",
            "retention_days": 7,
        },
    ) as cleanup:
        result = service.cleanup_logs(retention_days=7)

    cleanup.assert_called_once_with(
        agent_hash="abcd",
        retention_days=7,
        storage=service._vfs.storage,
    )
    assert result.startswith("OK:")
    assert "删除 2 个" in result
