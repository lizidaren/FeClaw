"""
VirtualFileSystem - 为 AI Agent 构建的虚拟文件系统
还原真实的 Linux bash 使用体验，所有命令操作 COS 上的虚拟路径空间
"""

import os
import re
import fnmatch
import hashlib
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from config import settings

# VFS modules - parser, executor, and builtin commands
from .vfs.tokens import Tokenizer
from .vfs.parser import Parser
from .vfs.executor import Executor
from .vfs.registry import get_registry
from .vfs.paths import (
    PathResolver,
    validate_filename as _validate_filename_impl,
    parse_cos_date as _parse_cos_date_impl,
    gen_inode as _gen_inode_impl,
    get_mode_from_name as _get_mode_from_name_impl,
    format_date as _format_date_impl,
    format_size as _format_size_impl,
    classify_suffix as _classify_suffix_impl,
    mode_to_octal as _mode_to_octal_impl,
    mode_to_perm_string as _mode_to_perm_string_impl,
    parse_cut_fields as _parse_cut_fields_impl,
)
from .vfs.cos_client import CosClient

logger = logging.getLogger(__name__)


@dataclass
class FileEntry:
    """文件条目数据结构"""
    name: str          # 文件名（不含路径）
    path: str          # 相对路径（相对于 base_path）
    type: str          # "file" | "directory" | "symlink"
    size: int          # 字节数（目录为 4096）
    mtime: float       # 修改时间戳（Unix epoch）
    mode: str          # 权限字符串如 "drwxr-xr-x" / "-rw-r--r--"
    inode: int         # inode 号（使用 ETag 的 hash）
    is_hidden: bool    # 是否隐藏（. 开头）
    nlink: int = 1     # 链接数

    @classmethod
    def from_cos_object(cls, key: str, rel_path: str, size: int, last_modified: str, etag: str) -> "FileEntry":
        """从 COS 对象创建 FileEntry"""
        name = os.path.basename(rel_path.rstrip("/"))
        is_dir = rel_path.endswith("/")
        is_hidden = name.startswith(".") if name else False
        mtime = cls._parse_cos_date(last_modified)

        if is_dir:
            ftype = "directory"
            perm = "drwxr-xr-x"
            nlink = 2  # . and ..
        else:
            ftype = "file"
            # 根据扩展名判断权限
            if name.endswith(".sh"):
                perm = "-rwxr-xr-x"
            else:
                perm = "-rw-r--r--"
            nlink = 1

        # 生成 inode（ETag 的 hash）
        inode = int(hashlib.md5(f"{key}{etag}".encode()).hexdigest()[:8], 16) % (10**8)

        return cls(
            name=name,
            path=rel_path.rstrip("/"),
            type=ftype,
            size=size if not is_dir else 4096,
            mtime=mtime,
            mode=perm,
            inode=inode,
            is_hidden=is_hidden,
            nlink=nlink
        )

    @staticmethod
    def _parse_cos_date(date_str: str) -> float:
        """解析 COS 的 LastModified 时间字符串为 Unix epoch"""
        return _parse_cos_date_impl(date_str)


@dataclass
class VirtualDirEntry:
    """虚拟目录条目（用于 ls 等命令的输出）"""
    name: str
    path: str
    type: str           # "file" | "directory"
    size: int
    mtime: float
    mode: str
    inode: int
    is_hidden: bool
    nlink: int = 1
    target: Optional[str] = None  # for symlinks


class VirtualFileSystem:
    """
    虚拟文件系统 - 操作 COS 上的虚拟路径空间

    路径规则：
    - /workspace/... 相对于 base_path 解析
    - ~ 表示 /workspace
    - .. 不能穿越 base_path

    支持 agent_id 绑定：
    - 如果指定 agent_id，路径格式为：agents/{agent_id}/workspace/
    - 如果不指定 agent_id，路径格式为：user_workspaces/{user_id}/workspace/
    """

    # 特殊目录
    WORKSPACE_DIR = "workspace"
    AGENT_DIR = "agent"

    # 日期格式（长格式用）
    DATE_FORMAT = "%b %d %H:%M"
    DATE_FORMAT_YEAR = "%b %d  %Y"

    def __init__(self, user_id: str = None, storage_service=None, agent_id: str = None, agent_hash: str = None):
        """
        初始化虚拟文件系统

        Args:
            user_id: 用户 ID（向后兼容，或从 agent_hash 获取）
            storage_service: 可选的存储服务实例（用于测试）
            agent_id: 可选的 Agent ID（4 位 hash），用于 Agent 绑定（已弃用，使用 agent_hash）
            agent_hash: Agent 的 4 位 hash（推荐）
        """
        # 支持 agent_hash 参数（推荐）
        if agent_hash:
            self.agent_id = agent_hash
        elif agent_id:
            self.agent_id = agent_id
        else:
            self.agent_id = None
        
        # 如果没有 user_id 但有 agent_hash，从数据库获取
        if user_id is None and self.agent_id:
            from models.database import SessionLocal, AgentProfile
            db = SessionLocal()
            try:
                agent = db.query(AgentProfile).filter(AgentProfile.hash == self.agent_id).first()
                if agent:
                    user_id = str(agent.user_id)
            finally:
                db.close()
        
        self.user_id = str(user_id) if user_id else ""

        # 根据 agent_id 设置 base_path
        if self.agent_id:
            # Agent 绑定模式：路径格式为 agents/{agent_id}/
            self.base_path = f"{settings.TENCENT_COS_PREFIX}agents/{self.agent_id}/"
        else:
            # 用户模式：路径格式为 user_workspaces/{user_id}/
            logger.warning("[DEPRECATED] VirtualFileSystem initialized without agent_id, using user_workspaces/ path. This path is deprecated, use agents/{hash}/ instead.")
            self.base_path = f"feclaw/user_workspaces/{self.user_id}/"

        self._cwd = ""  # 默认工作目录设为根目录（/），包含 agent/ 和 workspace/
        self._prev_cwd = ""  # 上一个工作目录（用于 cd -）
        self._storage = storage_service
        self._cos_client = None  # CosClient 实例（懒加载）
        self._dir_cache: Dict[str, List[VirtualDirEntry]] = {}  # 目录缓存

        # Initialize VFS command registry
        # Use global registry (all commands registered via register_all_commands)
        from .vfs.builtin import register_all_commands as _rac
        _rac()  # 确保所有命令已注册
        self._registry = get_registry()
        self._versioning = None  # VFSVersioningService 实例（懒加载）

    @property
    def storage(self):
        """懒加载 StorageService"""
        if self._storage is None:
            from services.storage_service import StorageService
            self._storage = StorageService()
        return self._storage

    @property
    def cos_client(self):
        """懒加载 CosClient"""
        if self._cos_client is None:
            from .vfs.cos_client import CosClient
            self._cos_client = CosClient(self._storage)
        return self._cos_client

    @property
    def versioning(self):
        """懒加载 VFSVersioningService"""
        if self._versioning is None:
            from services.vfs_versioning import VFSVersioningService
            self._versioning = VFSVersioningService(self.user_id, self._storage)
        return self._versioning

    # ========== 路径解析 ==========

    def _resolve_path(self, path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        将虚拟路径解析为 COS key

        Returns:
            (cos_key, error_msg)
            如果路径非法（如 .. 穿越），返回 (None, error_msg)
            对于 /config/xxx 路径，返回特殊标记用于数据库配置访问
        """
        if not path:
            return self.base_path, None

        if path == ".":
            if self._cwd:
                return f"{self.base_path}{self._cwd}", None
            return self.base_path, None

        path = path.strip()

        # 处理 ~ 展开为用户根目录
        if path == "~":
            path = ""
        elif path.startswith("~/"):
            path = path[2:]  # Remove ~/

        # 处理 /config/ 前缀（虚拟配置路径）
        normalized = path.lstrip("/")
        if normalized == "config" or normalized.startswith("config/"):
            # 返回特殊前缀标记，由调用者识别处理
            return ("__CONFIG__:" + normalized, None)

        # 处理绝对路径
        if path.startswith("/"):
            # 去掉前导 /
            path = path.lstrip("/")
            # 处理 /public/ 公共数据空间（只读共享目录）
            # 返回实际 COS key: {TENCENT_COS_PREFIX}public/{relpath}
            if path == "public" or path.startswith("public/"):
                subpath = normalized[7:] if normalized.startswith("public/") else ""
                # 安全检查：防止 .. 路径遍历（与下方统一逻辑保持一致）
                if ".." in subpath:
                    return (None, "Error: 路径不允许 ..")
                cos_key = self._get_public_base_path() + subpath
                return (cos_key, None)
        elif self._cwd:
            # 相对路径：加上当前目录
            path = f"{self._cwd}/{path}"

        # 处理 . 和 ..
        parts = []
        for part in path.split("/"):
            if part == "." or part == "":
                continue
            elif part == "..":
                # 不能穿越 base_path
                if not parts:
                    # 已经在根，拒绝穿越
                    return None, f"Error: 路径不允许 .. 穿越: {path}"
                parts.pop()
            else:
                parts.append(part)

        resolved = "/".join(parts)
        cos_key = f"{self.base_path}{resolved}" if resolved else self.base_path
        return cos_key, None

    def _resolve_to_dir_prefix(self, path: str) -> Tuple[str, Optional[str]]:
        """
        解析为目录前缀（确保以 / 结尾）
        """
        cos_key, err = self._resolve_path(path)
        if err:
            return "", err
        if not cos_key.endswith("/"):
            cos_key += "/"
        return cos_key, None

    def _get_cos_prefix(self, path: str = "") -> str:
        """获取 COS 前缀"""
        if not path:
            return self.base_path if not self._cwd else f"{self.base_path}{self._cwd}/"
        if path == ".":
            return self.base_path if not self._cwd else f"{self.base_path}{self._cwd}/"
        elif path.startswith("/"):
            resolved = path.lstrip("/")
            return f"{self.base_path}{resolved}/" if resolved else self.base_path
        elif path == "..":
            if not self._cwd:
                return self.base_path
            parent = "/".join(self._cwd.split("/")[:-1])
            return f"{self.base_path}{parent}/" if parent else self.base_path
        else:
            base = self.base_path if not self._cwd else f"{self.base_path}{self._cwd}/"
            return f"{base}{path}/"

    def _vpath_to_cos(self, vpath: str) -> str:
        """将虚拟路径转为 COS key（文件用，不加 /）"""
        cos_key, err = self._resolve_path(vpath)
        if err:
            return ""
        return cos_key

    def resolve_path(self, path: str):
        """Public API: resolve a virtual path to a COS key tuple (cos_key, error)."""
        return self._resolve_path(path)

    def read_file(self, path: str) -> str:
        """Public API: read a file by virtual path."""
        from services.file_locker import DistributedFileLock
        locker = DistributedFileLock()
        lock_path = self._resolve_path(path)[0]
        if not locker.acquire_read(self.user_id, lock_path, f"vfs-{self.user_id}", timeout=3.0):
            return "Error: 文件被其他进程锁定，暂时无法读取"
        try:
            return self._read_file(path)
        finally:
            locker.release(self.user_id, lock_path, f"vfs-{self.user_id}")

    # ========== 目录内容读取 ==========

    def _list_cos_dir(self, prefix: str) -> List[Dict]:
        """列出 COS prefix 下的直接子项"""
        return self.cos_client.list_objects(prefix)

    def _parse_dir_contents(self, prefix: str) -> List[VirtualDirEntry]:
        """
        解析目录内容，返回直接子项列表
        prefix: COS 前缀，必须以 / 结尾
        """
        if prefix in self._dir_cache:
            return self._dir_cache[prefix]

        objects = self._list_cos_dir(prefix)
        entries: Dict[str, VirtualDirEntry] = {}

        # 去掉前缀得到相对路径
        base_len = len(prefix)

        for obj in objects:
            key = obj["Key"]
            rel_path = key[base_len:].lstrip("/")

            if "/" in rel_path:
                # 子目录
                dir_name = rel_path.split("/")[0]
                dir_path = rel_path.split("/", 1)[0]

                if dir_name not in entries:
                    entries[dir_name] = VirtualDirEntry(
                        name=dir_name,
                        path=dir_path,
                        type="directory",
                        size=4096,
                        mtime=0,
                        mode="drwxr-xr-x",
                        inode=self._gen_inode(prefix + dir_name),
                        is_hidden=dir_name.startswith("."),
                        nlink=2
                    )
            else:
                # 文件
                name = rel_path
                if name not in entries:
                    entry = VirtualDirEntry(
                        name=name,
                        path=rel_path,
                        type="file",
                        size=int(obj.get("Size", 0) or 0),
                        mtime=FileEntry._parse_cos_date(obj.get("LastModified", "")),
                        mode=self._get_mode_from_name(name),
                        inode=self._gen_inode(key),
                        is_hidden=name.startswith("."),
                        nlink=1
                    )
                    entries[name] = entry

        result = list(entries.values())
        # 按名字排序，目录在前
        result.sort(key=lambda e: (e.type != "directory", e.name))
        self._dir_cache[prefix] = result
        return result

    def _gen_inode(self, key: str) -> int:
        """生成 inode"""
        return _gen_inode_impl(key)

    def _get_mode_from_name(self, name: str) -> str:
        """根据文件名获取权限字符串"""
        return _get_mode_from_name_impl(name)

    def _format_date(self, mtime: float) -> str:
        """格式化日期"""
        return _format_date_impl(mtime)

    def _format_size(self, size: int, human: bool = False) -> str:
        """格式化文件大小"""
        return _format_size_impl(size, human)

    def _get_nlink_for_dir(self, prefix: str) -> int:
        """获取目录的 nlink（子目录数 + 2）"""
        entries = self._parse_dir_contents(prefix)
        child_dirs = sum(1 for e in entries if e.type == "directory")
        return child_dirs + 2

    def _update_dir_nlink(self, entry: VirtualDirEntry, prefix: str):
        """更新目录条目的 nlink"""
        if entry.type == "directory":
            entry.nlink = self._get_nlink_for_dir(prefix + entry.name + "/")

    # ========== ls 命令 ==========

    def ls(self, path: str = "", show_all: bool = False, long_format: bool = False,
           human: bool = False, recursive: bool = False, classify: bool = False,
           sort_by: str = "name", reverse: bool = False,
           directory_only: bool = False, show_inode: bool = False,
           oneline: bool = False) -> str:
        """
        完整 ls 实现

        Args:
            path: 路径
            show_all: -a, 显示 . 和 ..
            long_format: -l, 长格式
            human: -h, 人可读大小
            recursive: -R, 递归
            classify: -F, 文件类型标识
            sort_by: 排序方式 (name/time/size)
            reverse: -r, 反向排序
            directory_only: -d, 只显示目录本身
            show_inode: -i, 显示 inode
            oneline: -1, 每行一个文件
        """
        logger.info(f"[VFS] ls: path={path}, show_all={show_all}, long_format={long_format}, "
                    f"human={human}, recursive={recursive}, classify={classify}, "
                    f"sort_by={sort_by}, reverse={reverse}, directory_only={directory_only}, "
                    f"show_inode={show_inode}, oneline={oneline}")
        # 处理 cd 后没有参数的情况
        if not path:
            target = "." if self._cwd else ""
        else:
            target = path

        # 检查是否是 /config/ 路径（虚拟配置目录）
        normalized_target = target.lstrip("/")
        if normalized_target == "config" or normalized_target.startswith("config/"):
            return self._ls_config(normalized_target)

        # 检查是否是 /public/ 路径（公共数据空间）
        if normalized_target == "public" or normalized_target.startswith("public/"):
            # 去掉 "public/" 前缀，只传子路径给 _ls_public
            subpath = normalized_target[7:] if normalized_target.startswith("public/") else ""
            return self._ls_public(subpath)

        # 解析路径
        cos_prefix, err = self._resolve_to_dir_prefix(target)
        if err:
            return err

        # -d: 显示目录本身
        if directory_only:
            return self._ls_single(cos_prefix.rstrip("/"), long_format, human, show_inode, classify)

        # 检查是文件还是目录
        cos_key, _ = self._resolve_path(target)
        is_file = False
        if cos_key and not cos_key.endswith("/"):
            # 检查是否是文件
            objs = self._list_cos_dir(cos_key.rstrip("/").rsplit("/", 1)[0] + "/")
            for obj in objs:
                if obj["Key"].rstrip("/") == cos_key.rstrip("/"):
                    is_file = True
                    break

        if is_file:
            return self._ls_single(cos_key, long_format, human, show_inode, classify)

        # 列出目录内容
        entries = self._parse_dir_contents(cos_prefix)

        # 在根目录时添加虚拟目录（public, config）
        if cos_prefix == self.base_path:
            # 检查 public 目录是否有内容
            public_files = self._get_public_files("")
            if public_files:
                public_entry = VirtualDirEntry(
                    name="public",
                    path="public",
                    type="directory",
                    size=4096,
                    mtime=0,
                    mode="drwxr-xr-x",
                    inode=self._gen_inode("__virtual_public__"),
                    is_hidden=False,
                    nlink=2
                )
                entries.append(public_entry)

            # 检查 config 是否有配置
            config_keys = self._list_config()
            if config_keys:
                config_entry = VirtualDirEntry(
                    name="config",
                    path="config",
                    type="directory",
                    size=4096,
                    mtime=0,
                    mode="drwxr-xr-x",
                    inode=self._gen_inode("__virtual_config__"),
                    is_hidden=False,
                    nlink=2
                )
                entries.append(config_entry)

        # 过滤隐藏文件
        if not show_all:
            entries = [e for e in entries if not e.is_hidden]
        elif show_all:
            # 添加 . 和 ..
            dot_entry = VirtualDirEntry(
                name=".", path="", type="directory", size=4096,
                mtime=0, mode="drwxr-xr-x", inode=0, is_hidden=False, nlink=2
            )
            dotdot_entry = VirtualDirEntry(
                name="..", path="", type="directory", size=4096,
                mtime=0, mode="drwxr-xr-x", inode=0, is_hidden=False, nlink=2
            )
            entries = [dot_entry, dotdot_entry] + entries

        if not entries:
            return ""

        # 递归模式
        if recursive:
            return self._ls_recursive(cos_prefix, entries, show_all, long_format, human,
                                       classify, sort_by, reverse, show_inode, oneline)

        # 排序
        entries = self._sort_entries(entries, sort_by, reverse)

        # 格式化输出
        return self._format_ls_output(entries, long_format, human, classify,
                                       show_inode, oneline)

    def _ls_single(self, cos_key: str, long_format: bool, human: bool,
                   show_inode: bool, classify: bool) -> str:
        """显示单个条目（文件或目录用于 -d）"""
        # 提取目录前缀
        if "/" in cos_key.rstrip("/"):
            dir_prefix = cos_key.rsplit("/", 1)[0] + "/"
            name = cos_key.rstrip("/").rsplit("/", 1)[-1]
        else:
            dir_prefix = self.base_path
            name = cos_key.rstrip("/")

        entries = self._parse_dir_contents(dir_prefix)
        for e in entries:
            if e.name == name:
                # 更新 nlink
                self._update_dir_nlink(e, dir_prefix)
                return self._format_ls_output([e], long_format, human, classify, show_inode, False)

        return f"Error: 文件不存在: {name}"

    def _ls_recursive(self, prefix: str, entries: List[VirtualDirEntry],
                      show_all: bool, long_format: bool, human: bool,
                      classify: bool, sort_by: str, reverse: bool,
                      show_inode: bool, oneline: bool) -> str:
        """递归 ls -R"""
        lines = []
        current_prefix = prefix.rstrip("/")

        # 显示当前目录头
        if self._cwd:
            display_path = "/" + self._cwd
        else:
            display_path = "/"
        if current_prefix != self.base_path.rstrip("/"):
            display_path = "/" + current_prefix[len(self.base_path):].rstrip("/")

        lines.append(f"{display_path}:")

        # 更新目录的 nlink
        for e in entries:
            if e.type == "directory":
                self._update_dir_nlink(e, prefix)

        # 排序并显示
        sorted_entries = self._sort_entries(entries, sort_by, reverse)
        lines.append(self._format_ls_output(sorted_entries, long_format, human,
                                            classify, show_inode, oneline))

        # 递归处理子目录
        for e in sorted_entries:
            if e.type == "directory":
                subdir_prefix = f"{prefix}{e.name}/"
                sub_entries = self._parse_dir_contents(subdir_prefix)
                if sub_entries or show_all:
                    sub_cos_prefix = f"{current_prefix}/{e.name}/" if current_prefix else f"{self.base_path}{e.name}/"
                    lines.append("")
                    lines.extend(self._ls_recursive(sub_cos_prefix, sub_entries,
                                                    show_all, long_format, human,
                                                    classify, sort_by, reverse,
                                                    show_inode, oneline).split("\n"))

        return "\n".join(lines)

    def _sort_entries(self, entries: List[VirtualDirEntry], sort_by: str, reverse: bool) -> List[VirtualDirEntry]:
        """排序条目"""
        if sort_by == "time":
            entries.sort(key=lambda e: e.mtime, reverse=not reverse)
        elif sort_by == "size":
            entries.sort(key=lambda e: e.size, reverse=not reverse)
        else:  # name
            entries.sort(key=lambda e: e.name.lower(), reverse=reverse)
            # 目录排前面
            entries.sort(key=lambda e: e.type != "directory")
        return entries

    def _format_ls_output(self, entries: List[VirtualDirEntry], long_format: bool,
                          human: bool, classify: bool, show_inode: bool,
                          oneline: bool) -> str:
        """格式化 ls 输出"""
        if not entries:
            return ""

        if long_format:
            return self._format_ls_long(entries, human, show_inode)
        elif oneline:
            return "\n".join(e.name + self._classify_suffix(e) if classify else e.name
                            for e in entries)
        else:
            # 普通格式，按列排列
            names = [e.name + self._classify_suffix(e) if classify else e.name
                    for e in entries]
            return "  ".join(names)

    def _format_ls_long(self, entries: List[VirtualDirEntry], human: bool, show_inode: bool) -> str:
        """格式化 ls -l 输出"""
        lines = []
        total_blocks = sum(e.size for e in entries) // 512 + 1
        lines.append(f"总计 {total_blocks}")

        for e in entries:
            # 权限
            mode = e.mode

            # inode
            inode_str = f"{e.inode:>8} " if show_inode else ""

            # nlink
            nlink_str = str(e.nlink)

            # owner/group（固定为 user user）
            owner = "user"
            group = "user"

            # size
            size_str = self._format_size(e.size, human) if e.type == "file" else "4096"
            size_str = f"{size_str:>6}"

            # date
            date_str = self._format_date(e.mtime)

            # name
            name = e.name
            if e.type == "directory":
                name += "/"
            elif e.type == "symlink" and e.target:
                name += f" -> {e.target}"

            suffix = self._classify_suffix(e)
            if suffix and not name.endswith(suffix):
                name += suffix

            line = f"{inode_str}{mode}  {nlink_str} {owner} {group} {size_str} {date_str} {name}"
            lines.append(line)

        return "\n".join(lines)

    def _classify_suffix(self, entry) -> str:
        """-F 标识符"""
        return _classify_suffix_impl(entry)

    # ========== cd 命令 ==========

    def cd(self, path: str = "") -> Tuple[bool, str]:
        """
        切换工作目录

        Args:
            path: 目标路径

        Returns:
            (success, error_message)
        """
        if not path or path == "~":
            # cd ~ 或 cd（默认）→ workspace
            self._prev_cwd = self._cwd
            self._cwd = ""
            return True, ""

        if path == "-":
            # cd - → 上一个目录
            if not self._prev_cwd:
                self._prev_cwd = ""
            self._cwd, self._prev_cwd = self._prev_cwd, self._cwd
            return True, ""

        # 处理 ..
        if path == "..":
            if not self._cwd:
                return True, ""  # 已在根
            parts = self._cwd.split("/")
            if len(parts) == 1:
                self._cwd = ""
            else:
                self._cwd = "/".join(parts[:-1])
            return True, ""

        # 相对路径
        if path.startswith("/"):
            # 绝对路径（相对于 base_path）
            resolved = path.lstrip("/")
            # 检查 .. 穿越
            parts = []
            for part in resolved.split("/"):
                if part == "." or part == "":
                    continue
                elif part == "..":
                    if parts:
                        parts.pop()
                    # else: 穿越根，拒绝
                else:
                    parts.append(part)
            new_cwd = "/".join(parts)
            # 验证路径存在：查 new_cwd 的父目录
            # new_cwd 的父目录路径（相对于 base_path）
            parent_cwd = "/".join(new_cwd.split("/")[:-1]) if "/" in new_cwd else ""
            parent_prefix = f"{self.base_path}{parent_cwd}/" if parent_cwd else self.base_path
            parent_entries = self._parse_dir_contents(parent_prefix)
            target_name = new_cwd.split("/")[-1]  # 最后一个路径段
            found = any(e.name == target_name and e.type == "directory" for e in parent_entries)
            if not found:
                return False, f"Error: 目录不存在: {path}"
            self._prev_cwd = self._cwd
            self._cwd = new_cwd
            return True, ""
        else:
            # 相对路径
            if self._cwd:
                new_cwd = f"{self._cwd}/{path}"
            else:
                new_cwd = path

            # 验证路径存在：查父目录中是否有同名目录项
            parent_cos_prefix = f"{self.base_path}{self._cwd}/" if self._cwd else self.base_path
            parent_entries = self._parse_dir_contents(parent_cos_prefix)
            target_name = new_cwd.split("/")[-1]
            found = any(e.name == target_name and e.type == "directory" for e in parent_entries)
            if not found:
                return False, f"Error: 目录不存在: {path}"
            self._prev_cwd = self._cwd
            self._cwd = new_cwd
            return True, ""

    # ========== pwd 命令 ==========

    def pwd(self) -> str:
        """返回当前工作目录"""
        if self._cwd:
            return f"/{self._cwd}"
        return "/"

    # ========== cat 命令 ==========

    def cat(self, *paths: str) -> str:
        """连接输出多个文件"""
        logger.info(f"[VFS] cat: paths={paths}")
        if not paths:
            return "Error: 用法: cat <文件>..."

        results = []
        for p in paths:
            content = self._read_file(p)
            if content.startswith("Error:"):
                logger.info(f"[VFS] cat result: error for path={p}")
                return content
            results.append(content)
            logger.info(f"[VFS] cat result: path={p}, content_len={len(content)}, truncated={content[:200] if len(content) > 200 else content}")

        logger.info(f"[VFS] cat result: total_paths={len(paths)}, total_content_len={sum(len(r) for r in results)}")
        return "\n".join(results)

    def _read_file(self, path: str) -> str:
        """读取单个文件内容"""
        cos_key, err = self._resolve_path(path)
        if err:
            return err

        # 处理虚拟配置路径
        if cos_key.startswith("__CONFIG__:"):
            config_key = cos_key.replace("__CONFIG__:", "")
            return self._read_config(config_key)

        # 处理公共数据空间路径
        if self._is_public_path(cos_key):
            # cos_key = {TENCENT_COS_PREFIX}public/{relpath}
            public_base = self._get_public_base_path()
            public_rel = cos_key[len(public_base):]
            return self._read_public_file(public_rel)

        try:
            content = self.storage.get_file_content(cos_key)
            if content is None:
                return f"Error: 文件不存在: {path}"
            return content.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error: 读取失败: {e}"

    # ========== Async wrappers (avoid blocking event loop on COS I/O) ==========

    async def async_cat(self, *paths: str) -> str:
        """异步版本的 cat — 在线程池执行，不阻塞事件循环"""
        return await asyncio.to_thread(self.cat, *paths)

    async def async_write(self, path: str, content: str) -> str:
        """异步写入文件 (覆盖) — 在线程池执行 echo，不阻塞事件循环"""
        return await asyncio.to_thread(self.echo, content, path, False)

    async def async_ls(self, path: str = "", show_all: bool = False, long_format: bool = False,
                       human: bool = False, recursive: bool = False, classify: bool = False,
                       sort_by: str = "name", reverse: bool = False,
                       directory_only: bool = False, show_inode: bool = False,
                       oneline: bool = False) -> str:
        """异步版本的 ls — 在线程池执行，不阻塞事件循环"""
        return await asyncio.to_thread(
            self.ls, path, show_all=show_all, long_format=long_format,
            human=human, recursive=recursive, classify=classify,
            sort_by=sort_by, reverse=reverse, directory_only=directory_only,
            show_inode=show_inode, oneline=oneline,
        )

    async def async_read_file(self, path: str) -> str:
        """异步读取单个文件 — 在线程池执行，不阻塞事件循环"""
        return await asyncio.to_thread(self._read_file, path)

    def _vpath_to_config_key(self, vpath: str) -> str:
        """将 VFS 相对路径转为 DB config key

        sandbox 模式下根级文件（persona, tools 等）→ agents/{hash}/{key}
        其他路径（global/xxx, channels/xxx/xxx 等）直接返回
        """
        if self.agent_id and "/" not in vpath:
            return f"agents/{self.agent_id}/{vpath}"
        return vpath

    def _read_config(self, key: str) -> str:
        """从数据库读取配置（按 agent_hash+key 过滤，跳过 permission=none）"""
        # 去掉 config/ 前缀
        config_key = key.replace("config/", "") if key.startswith("config/") else key
        # 映射新格式 DB key
        db_key = self._vpath_to_config_key(config_key)
        try:
            from models.database import AgentConfig, SessionLocal
            db = SessionLocal()
            try:
                q = db.query(AgentConfig).filter(
                    AgentConfig.key == db_key,
                    AgentConfig.permission != "none"
                )
                if self.agent_id and db_key.startswith("agents/"):
                    q = q.filter(AgentConfig.agent_hash == self.agent_id)
                config = q.first()
                if config:
                    return config.value
                # 回退：尝试旧格式 key
                old_keys = []
                # 如果 config_key 是 channels/{channel}/{name} 格式，也尝试 config.{channel}.{name}
                if config_key.startswith("channels/"):
                    parts = config_key.split("/", 2)
                    if len(parts) >= 3:
                        old_keys.append(f"config.{parts[1]}.{parts[2]}")
                old_keys.append(f"config.global.{config_key}")
                for old_key in old_keys:
                    config = db.query(AgentConfig).filter(
                        AgentConfig.key == old_key,
                        AgentConfig.permission != "none"
                    ).first()
                    if config:
                        return config.value
                return f"Error: 配置不存在: {config_key}"
            finally:
                db.close()
        except Exception as e:
            return f"Error: 读取配置失败: {e}"

    def _write_config(self, key: str, value: str) -> bool:
        """写入配置到数据库（按 agent_hash+key 过滤，检查权限）"""
        # 去掉 config/ 前缀
        config_key = key.replace("config/", "") if key.startswith("config/") else key
        # 映射新格式 DB key
        db_key = self._vpath_to_config_key(config_key)
        try:
            from models.database import AgentConfig, SessionLocal
            db = SessionLocal()
            try:
                q = db.query(AgentConfig).filter(AgentConfig.key == db_key)
                if self.agent_id and db_key.startswith("agents/"):
                    q = q.filter(AgentConfig.agent_hash == self.agent_id)
                config = q.first()
                # 权限检查：read 和 none 不允许写入
                if config and config.permission in ("read", "none"):
                    logger.warning(f"[VFS] _write_config denied: {db_key} permission={config.permission}")
                    return False
                if config:
                    config.value = value
                else:
                    agent_hash = self.agent_id if db_key.startswith("agents/") else None
                    config = AgentConfig(key=db_key, value=value,
                                         agent_hash=agent_hash)
                    db.add(config)
                db.commit()
                return True
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[VFS] _write_config error: {e}")
            return False

    def _list_config(self) -> list:
        """列出所有配置（按 agent_hash 过滤，跳过 permission=none）"""
        try:
            from models.database import AgentConfig, SessionLocal
            db = SessionLocal()
            try:
                q = db.query(AgentConfig).filter(AgentConfig.permission != "none")
                if self.agent_id:
                    # sandbox 模式：获取 agent 私有 + 全局 + 渠道配置
                    from sqlalchemy import or_
                    q = q.filter(or_(
                        AgentConfig.agent_hash == self.agent_id,
                        AgentConfig.agent_hash == None,
                    ))
                configs = q.all()
                # 显示格式：将 agents/{hash}/xxx → xxx，保持简洁
                keys = []
                for c in configs:
                    k = c.key
                    if k.startswith(f"agents/{self.agent_id}/"):
                        k = k[len(f"agents/{self.agent_id}/"):]
                    keys.append(k)
                return keys
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[VFS] _list_config error: {e}")
            return []

    def _ls_config(self, path: str = "") -> str:
        """列出配置目录内容（按 agent_hash 过滤）"""
        configs = self._list_config()

        # 如果是 ls /config/xxx（具体配置），检查是否存在
        if path and path != "config" and path != "config/":
            config_key = path.replace("config/", "")
            # 对于无 / 的 key（agent 级），映射到 agents/{hash}/{key}
            db_key = self._vpath_to_config_key(config_key)
            if config_key in configs:
                from models.database import AgentConfig, SessionLocal
                db = SessionLocal()
                try:
                    q = db.query(AgentConfig).filter(AgentConfig.key == db_key)
                    if self.agent_id and db_key.startswith("agents/"):
                        q = q.filter(AgentConfig.agent_hash == self.agent_id)
                    config = q.first()
                    if config:
                        return f"-rw-r--r-- 1 user user {len(config.value or '')} {config.updated_at.strftime('%Y-%m-%d %H:%M:%S') if config.updated_at else ''} {config.key}"
                finally:
                    db.close()

        # 列出所有配置
        if not configs:
            return ""

        result = []
        for name in sorted(configs):
            result.append(f"drwxr-xr-x 1 user user 4096 {name}/")

        if len(result) == 1:
            return result[0]
        return "\n".join(result)

    # ========== /public/ 公共数据空间 ==========

    def _get_public_base_path(self) -> str:
        """获取公共数据空间的 COS 前缀"""
        return f"{settings.TENCENT_COS_PREFIX}public/"

    def _is_public_path(self, cos_key: str) -> bool:
        """检查 cos_key 是否属于 /public/ 公共空间"""
        public_base = self._get_public_base_path()
        return cos_key.startswith(public_base)

    def _get_public_files(self, prefix: str = "") -> List[Dict]:
        """列出公共目录下的文件"""
        cos_prefix = self._get_public_base_path()
        if prefix:
            cos_prefix = cos_prefix + prefix.lstrip("/")
        if not cos_prefix.endswith("/"):
            cos_prefix += "/"
        return self.cos_client.list_objects_raw(
            settings.TENCENT_COS_BUCKET, cos_prefix, max_keys=1000
        )

    def _ls_public(self, path: str = "") -> str:
        """
        列出 /public/ 目录内容

        Args:
            path: 相对于 /public/ 的路径，如 ""、"docs/"、"docs/readme.md"
        """
        logger.info(f"[VFS] _ls_public: path={path}")

        # 获取公共文件列表（使用 /public/ 子路径）
        objects = self._get_public_files(path)

        if not objects:
            # 尝试作为文件读取
            cos_key = self._get_public_base_path() + path.lstrip("/")
            try:
                content = self.storage.get_file_content(cos_key)
                if content is not None:
                    # 是文件，显示单文件信息
                    return self._format_public_file_info(cos_key, path.split("/")[-1], len(content))
            except Exception:
                pass
            return f"/public/{path}: No such file or directory"
        
        # 分析对象，构建目录条目
        # 注意：rel_path 应该相对于请求的 path 计算，而不是相对于 public_base
        public_base = self._get_public_base_path()
        # 计算 COS 前缀（与 _get_public_files 逻辑一致）
        cos_prefix = public_base + path.lstrip("/")
        if not cos_prefix.endswith("/"):
            cos_prefix += "/"
        
        entries: Dict[str, Dict] = {}
        
        for obj in objects:
            key = obj["Key"]
            # 相对于请求的 cos_prefix 计算 rel_path
            rel_path = key[len(cos_prefix):]
            
            if "/" in rel_path:
                # 子目录
                dir_name = rel_path.split("/")[0]
                if dir_name and dir_name not in entries:
                    entries[dir_name] = {
                        "name": dir_name,
                        "type": "directory",
                        "size": 4096,
                        "mtime": 0
                    }
            else:
                # 文件
                name = rel_path
                if name:
                    size_val = obj.get("Size", 0)
                    if isinstance(size_val, str):
                        size_val = int(size_val) if size_val.isdigit() else 0
                    entries[name] = {
                        "name": name,
                        "type": "file",
                        "size": size_val,
                        "mtime": FileEntry._parse_cos_date(obj.get("LastModified", ""))
                    }
        
        if not entries:
            return f"/public/{path}: No such file or directory"
        
        # 排序：目录在前，名字排序
        result = []
        for name in sorted(entries.keys()):
            e = entries[name]
            if e["type"] == "directory":
                result.append(f"drwxr-xr-x  1 user user  4096 {name}/")
            else:
                size_str = self._format_size(e["size"], True)
                date_str = self._format_date(e["mtime"])
                result.append(f"-rw-r--r--  1 user user {size_str:>6} {date_str} {name}")
        
        return "\n".join(result)

    def _format_public_file_info(self, key: str, name: str, size: int) -> str:
        """格式化公共文件信息"""
        date_str = self._format_date(0)
        return f"-rw-r--r--  1 user user {size:>6} {date_str} {name}"

    def _read_public_file(self, rel_path: str) -> str:
        """
        读取公共文件内容
        
        Args:
            rel_path: 相对于 /public/ 的路径，如 "docs/guide.md"
        """
        logger.info(f"[VFS] _read_public_file: rel_path={rel_path}")
        
        cos_key = self._get_public_base_path() + rel_path.lstrip("/")
        
        try:
            content = self.storage.get_file_content(cos_key)
            if content is None:
                return f"Error: 文件不存在: /public/{rel_path}"
            return content.decode("utf-8", errors="replace")
        except Exception as e:
            logger.error(f"[VFS] _read_public_file error: {e}")
            return f"Error: 读取失败: {e}"

    # ========== head / tail ==========

    def head(self, path: str, n: int = 10) -> str:
        """显示文件前 N 行"""
        logger.info(f"[VFS] head: path={path}, n={n}")
        content = self._read_file(path)
        if content.startswith("Error:"):
            return content

        lines = content.split("\n")
        result = "\n".join(lines[:n])
        logger.info(f"[VFS] head result: {result[:300] if len(result) > 300 else result}")
        return result

    def tail(self, path: str, n: int = 10) -> str:
        """显示文件末 N 行"""
        logger.info(f"[VFS] tail: path={path}, n={n}")
        content = self._read_file(path)
        if content.startswith("Error:"):
            return content

        lines = content.split("\n")
        result = "\n".join(lines[-n:])
        logger.info(f"[VFS] tail result: {result[:300] if len(result) > 300 else result}")
        return result

    # ========== echo 重定向 ==========

    def echo(self, text: str, path: str, append: bool = False) -> str:
        """
        输出文本，支持重定向

        Args:
            text: 要输出的文本
            path: 目标文件路径
            append: True = >> (追加), False = > (覆盖)
        """
        logger.info(f"[VFS] echo: text={text[:100] if len(text) > 100 else text}, path={path}, append={append}")
        
        # 验证文件名
        import os
        filename = os.path.basename(path)
        err = self._validate_filename(filename)
        if err:
            return f"Error: {err}"
        
        cos_key, err = self._resolve_path(path)
        if err:
            return err

        # 禁止写入 /public/ 目录
        if self._is_public_path(cos_key):
            return f"Error: /public/ is a read-only system directory"

        # 处理虚拟配置路径
        if cos_key.startswith("__CONFIG__:"):
            config_key = cos_key.replace("__CONFIG__:", "")
            if append:
                # 追加：先读取现有内容
                existing_content = self._read_config(config_key)
                if existing_content.startswith("Error:"):
                    content = text + "\n"
                else:
                    content = existing_content + text + "\n"
            else:
                content = text + "\n"
            if self._write_config(config_key, content):
                return f"OK: {'追加写入' if append else '写入'} {path}"
            else:
                return f"Error: 写入配置失败"

        try:
            if append:
                # 追加：先读取现有内容
                existing = self.storage.get_file_content(cos_key)
                if existing:
                    content = existing.decode("utf-8", errors="replace") + text + "\n"
                else:
                    content = text + "\n"
            else:
                # 覆盖写入：先快照现有内容（版本控制）
                existing = self.storage.get_file_content(cos_key)
                if existing:
                    # 保存当前版本快照
                    try:
                        self.versioning.auto_snapshot(path, existing, comment="before_overwrite")
                        logger.info(f"[VFS] 已创建版本快照: {path}")
                    except Exception as ve:
                        logger.warning(f"[VFS] 版本快照失败: {ve}")
                content = text + "\n"

            from services.file_locker import DistributedFileLock

            # 获取文件写锁（防止多 SubAgent 同时写入同一文件）
            locker = DistributedFileLock()
            lock_owner = f"vfs-echo-{self.user_id or self.agent_id or 'anon'}"
            lock_acquired = False
            for retry in range(5):  # 最多重试 5 次
                if locker.acquire_write(self.user_id or "", cos_key, lock_owner, timeout=10.0):
                    lock_acquired = True
                    break
                if retry < 4:
                    import time as _time
                    _time.sleep(0.5)  # 等 0.5 秒后重试
                    logger.debug(f"[VFS] echo 写锁争用，重试 {retry + 1}/5: {path}")

            self.storage.put_object(cos_key, content.encode("utf-8"))

            if lock_acquired:
                locker.release(self.user_id or "", cos_key, lock_owner)

            # 写入成功后注册新版本
            try:
                self.versioning.register_version(path, content.encode("utf-8"), 
                                                  comment="append" if append else "write")
                logger.info(f"[VFS] 已注册新版本: {path}")
            except Exception as ve:
                logger.warning(f"[VFS] 版本注册失败: {ve}")

            # 异步触发向量化（非阻塞，不干扰写入流程）
            if self._should_vectorize(path, append):
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(self._async_index_file(path))
                except RuntimeError:
                    # 无事件循环（同步上下文），开新线程跑
                    import threading
                    _agent = self.agent_id or ""
                    _path = path
                    def _run_index():
                        from services.vfs_indexer import VfsIndexer
                        asyncio.run(VfsIndexer(agent_hash=_agent, file_path=_path).run())
                    threading.Thread(target=_run_index, daemon=True).start()

            return f"OK: 已写入 {len(content)} 字符到 {path}"

        except Exception as e:
            logger.error(f"[VFS] 写入失败: {e}")
            return f"Error: 写入失败: {e}"

    def _should_vectorize(self, path: str, append: bool = False) -> bool:
        """判断文件是否应自动向量化"""
        if append:
            return False  # 追加操作暂不触发
        # 排除 Agent 基础配置文件（人设/引导，不是知识库）
        _blacklist = {"soul.md", "identity.md", "user.md", "memory.md", "BOOTSTRAP.md"}
        basename = os.path.basename(path)
        if basename in _blacklist:
            return False
        ext = os.path.splitext(path)[1].lower()
        return ext in (".md", ".txt")

    def _async_index_file(self, path: str):
        """异步触发 VFS 文件索引（fire-and-forget）"""
        try:
            from services.vfs_indexer import VfsIndexer
            agent_hash = self.agent_id or ""
            indexer = VfsIndexer(agent_hash=agent_hash, file_path=path)
            asyncio.create_task(indexer.run())
            logger.info(f"[VFS] 已触发异步索引: {path}")
        except Exception as e:
            logger.warning(f"[VFS] 触发异步索引失败: {e}")

    # ========== grep ==========

    def grep(self, pattern: str, *paths: str,
             ignore_case: bool = False, show_line_no: bool = True,
             invert: bool = False, recursive: bool = False,
             name_only: bool = False, only_matching: bool = False) -> str:
        """
        搜索文件内容

        Args:
            pattern: 搜索模式
            paths: 文件路径
            ignore_case: -i 忽略大小写
            show_line_no: -n 显示行号
            invert: -v 反向匹配
            recursive: -r 递归搜索
            name_only: -l 只显示文件名
            only_matching: -o 只输出匹配的部分
        """
        logger.info(f"[VFS] grep: pattern={pattern}, paths={paths}, ignore_case={ignore_case}, "
                    f"show_line_no={show_line_no}, invert={invert}, recursive={recursive}, name_only={name_only}")
        if not paths:
            return "Error: 用法: grep <pattern> <file>..."

        if recursive:
            result = self._grep_recursive(pattern, paths[0], ignore_case, show_line_no, invert, name_only, only_matching)
            logger.info(f"[VFS] grep result (recursive): {result[:500] if len(result) > 500 else result}")
            return result

        results = []
        for p in paths:
            result = self._grep_file(pattern, p, ignore_case, show_line_no, invert, name_only, only_matching)
            if result.startswith("Error:"):
                logger.info(f"[VFS] grep result: error for path={p}")
                return result
            if result:
                results.append(result)

        final_result = "\n".join(results) if results else ""
        logger.info(f"[VFS] grep result: matched_files={len(results)}, result_len={len(final_result)}, "
                    f"truncated={final_result[:300] if len(final_result) > 300 else final_result}")
        return final_result

    def _grep_file(self, pattern: str, path: str,
                   ignore_case: bool, show_line_no: bool, invert: bool,
                   name_only: bool = False, only_matching: bool = False,
                   resolved_key: str = None) -> str:
        """在单个文件中搜索"""
        if resolved_key:
            cos_key = resolved_key
        else:
            cos_key, err = self._resolve_path(path)
            if err:
                return err

        try:
            content = self.storage.get_file_content(cos_key)
        except Exception as e:
            return f"Error: 读取失败: {e}"

        if content is None:
            return f"Error: 文件不存在: {path}"
        content = content.decode("utf-8", errors="replace")

        if ignore_case:
            pattern = pattern.lower()

        lines = content.split("\n")
        matching = []

        for i, line in enumerate(lines, 1):
            test_line = line if not ignore_case else line.lower()
            if only_matching:
                # -o: 输出匹配的部分，每个匹配一行
                found_count = test_line.count(pattern)
                if found_count > 0:
                    if invert:
                        # -v: 输出不包含匹配的行（罕见组合，输出整行）
                        if show_line_no:
                            matching.append(f"{i}:{line}")
                        else:
                            matching.append(line)
                    else:
                        # 每个匹配输出一行
                        for _ in range(found_count):
                            matching.append(pattern)
            else:
                # 普通模式：输出匹配的行
                found = pattern in test_line
                if invert:
                    found = not found
                if found:
                    if name_only:
                        return path
                    if show_line_no:
                        matching.append(f"{i}:{line}")
                    else:
                        matching.append(line)

        return "\n".join(matching)

    def _grep_recursive(self, pattern: str, dir_path: str,
                        ignore_case: bool, show_line_no: bool,
                        invert: bool, name_only: bool, only_matching: bool = False) -> str:
        """递归 grep"""
        cos_prefix, err = self._resolve_to_dir_prefix(dir_path)
        if err:
            return err

        results = []
        objects = self._list_cos_dir(cos_prefix)

        for obj in objects:
            key = obj["Key"]
            rel_path = key[len(cos_prefix):]

            # 只搜索文件
            if key.endswith("/"):
                continue

            result = self._grep_file(pattern, rel_path, ignore_case, show_line_no, invert, name_only, only_matching, resolved_key=key)
            if result and not result.startswith("Error:"):
                if name_only:
                    results.append(result)
                else:
                    results.append(f"{rel_path}:{result}")

        return "\n".join(results) if results else ""

    # ========== find ==========

    def find(self, path: str = "", name_pattern: str = "",
             find_type: str = "", recursive: bool = True) -> str:
        """
        查找文件

        Args:
            path: 搜索路径
            name_pattern: -name 模式（如 *.md）
            find_type: -type d/f
            recursive: 是否递归
        """
        logger.info(f"[VFS] find: path={path}, name_pattern={name_pattern}, find_type={find_type}, recursive={recursive}")
        if not path:
            path = "."

        cos_prefix, err = self._resolve_to_dir_prefix(path)
        if err:
            return err

        objects = self._list_cos_dir(cos_prefix)
        if not objects:
            logger.info("[VFS] find result: no objects found")
            return ""

        base_len = len(cos_prefix)
        results = []

        for obj in objects:
            key = obj["Key"]
            rel_path = key[base_len:].lstrip("/")

            if not rel_path:
                continue

            # 判断类型
            is_dir = rel_path.endswith("/")
            is_file = not is_dir

            # name 过滤
            if name_pattern:
                name = rel_path.rstrip("/").split("/")[-1]
                # 模式匹配（fnmatch）
                if not fnmatch.fnmatch(name, name_pattern):
                    # 也检查完整路径
                    if not fnmatch.fnmatch(rel_path.rstrip("/"), name_pattern):
                        continue

            # type 过滤
            if find_type == "d" and not is_dir:
                continue
            if find_type == "f" and not is_file:
                continue

            results.append(rel_path.rstrip("/"))

        final_result = "\n".join(sorted(results))
        logger.info(f"[VFS] find result: found={len(results)}, result={final_result[:300] if len(final_result) > 300 else final_result}")
        return final_result

    # ========== touch ==========

    def _validate_filename(self, name: str) -> str:
        """验证文件名是否合法"""
        return _validate_filename_impl(name)

    def touch(self, path: str) -> str:
        """创建空文件或更新 mtime"""
        logger.info(f"[VFS] touch: path={path}")
        
        # 验证文件名
        import os
        filename = os.path.basename(path)
        err = self._validate_filename(filename)
        if err:
            return f"Error: {err}"
        
        cos_key, err = self._resolve_path(path)
        if err:
            return err

        from services.file_locker import DistributedFileLock
        locker = DistributedFileLock()
        if not locker.acquire_write(self.user_id, cos_key, f"vfs-{self.user_id}", timeout=3.0):
            return "Error: 文件被其他进程锁定，暂时无法操作"
        try:
            # 检查文件是否存在
            existing = self.storage.get_file_content(cos_key)
            if existing is None:
                # 创建空文件
                self.storage.put_object(cos_key, b"")
                return f"OK: 已创建 {path}"
            else:
                # 更新 mtime（实际上 COS 不支持直接更新 mtime，但命令执行成功）
                return f"OK: 已更新 {path}"
        except Exception as e:
            return f"Error: touch 失败: {e}"
        finally:
            locker.release(self.user_id, cos_key, f"vfs-{self.user_id}")

    # ========== mkdir ==========

    def mkdir(self, path: str, parents: bool = False) -> str:
        """
        创建目录

        Args:
            path: 目录路径
            parents: -p 递归创建父目录
        """
        logger.info(f"[VFS] mkdir: path={path}, parents={parents}")
        
        # 禁止在 /public/ 下创建目录
        normalized = path.lstrip("/")
        if normalized == "public" or normalized.startswith("public/"):
            return f"Error: /public/ is a read-only system directory"
        
        # 验证目录名
        import os
        dirname = os.path.basename(path.rstrip("/"))
        err = self._validate_filename(dirname)
        if err:
            return f"Error: {err}"

        cos_key, err = self._resolve_path(path)
        if err:
            return err

        from services.file_locker import DistributedFileLock
        locker = DistributedFileLock()
        if not locker.acquire_write(self.user_id, cos_key, f"vfs-{self.user_id}", timeout=3.0):
            return "Error: 文件被其他进程锁定，暂时无法创建目录"
        try:
            if parents:
                result = self._mkdir_p(path)
                logger.info(f"[VFS] mkdir result: {result}")
                return result
            else:
                result = self._mkdir_single(path)
                logger.info(f"[VFS] mkdir result: {result}")
                return result
        finally:
            locker.release(self.user_id, cos_key, f"vfs-{self.user_id}")

    def _mkdir_single(self, path: str) -> str:
        """创建单个目录"""
        cos_key, err = self._resolve_path(path)
        if err:
            return err

        # 在 COS 中，目录用空对象表示（key 以 / 结尾）
        # 但实际上 COS 没有真正的目录，我们通过在父目录创建一个标记文件来模拟
        # 这里我们创建一个 .directory 标记文件
        dir_key = cos_key.rstrip("/") + "/.directory"

        try:
            self.storage.put_object(dir_key, b"")
            # 清除缓存
            prefix = cos_key.rsplit("/", 1)[0] + "/" if "/" in cos_key else self.base_path
            self._dir_cache.pop(prefix, None)
            return f"OK: 目录 {path}/ 已创建"
        except Exception as e:
            return f"Error: mkdir 失败: {e}"

    def _mkdir_p(self, path: str) -> str:
        """递归创建目录"""
        parts = path.strip("/").split("/")
        created = []

        for i in range(len(parts)):
            partial = "/".join(parts[:i+1])
            result = self._mkdir_single(partial)
            if result.startswith("Error:") and "已存在" not in result:
                return result
            created.append(partial)

        return f"OK: 目录 {path}/ 已创建"

    # ========== rm ==========

    def rm(self, path: str, recursive: bool = False, force: bool = False) -> str:
        """
        删除文件或目录

        Args:
            path: 路径
            recursive: -r 递归删除
            force: -f 强制删除
        """
        logger.info(f"[VFS] rm: path={path}, recursive={recursive}, force={force}")
        cos_key, err = self._resolve_path(path)
        if err:
            return err

        # 禁止删除 /public/ 文件
        if self._is_public_path(cos_key):
            return f"Error: /public/ is a read-only system directory"

        from services.file_locker import DistributedFileLock
        locker = DistributedFileLock()
        if not locker.acquire_write(self.user_id, cos_key, f"vfs-{self.user_id}", timeout=3.0):
            return "Error: 文件被其他进程锁定，暂时无法删除"
        try:
            # 检查是文件还是目录
            cos_prefix, _ = self._resolve_to_dir_prefix(path)
            entries = self._parse_dir_contents(cos_prefix)
            name = path.strip("/").split("/")[-1]
            target_entry = next((e for e in entries if e.name == name), None)

            if target_entry and target_entry.type == "directory":
                if not recursive:
                    return f"Error: 是目录（使用 rm -r 删除）: {path}"
                return self._rm_recursive(cos_key)
            else:
                return self._rm_file(cos_key)
        finally:
            locker.release(self.user_id, cos_key, f"vfs-{self.user_id}")

    def _rm_file(self, cos_key: str) -> str:
        """删除单个文件"""
        try:
            self.storage.delete_file_by_key(cos_key)
            return f"OK: 已删除 {cos_key}"
        except Exception as e:
            return f"Error: rm 失败: {e}"

    def _rm_recursive(self, cos_prefix: str) -> str:
        """递归删除目录"""
        try:
            objects = self._list_cos_dir(cos_prefix)
            for obj in objects:
                self.storage.delete_file_by_key(obj["Key"])
            # 删除目录标记
            dir_marker = cos_prefix.rstrip("/") + "/.directory"
            try:
                self.storage.delete_file_by_key(dir_marker)
            except Exception:
                logger.warning(f"Failed to delete directory marker: {dir_marker}")
            return f"OK: 已递归删除 {cos_prefix}"
        except Exception as e:
            return f"Error: rm -r 失败: {e}"

    # ========== rmdir ==========

    def rmdir(self, path: str) -> str:
        """删除空目录"""
        logger.info(f"[VFS] rmdir: path={path}")
        cos_key, err = self._resolve_path(path)
        if err:
            return err

        # 禁止删除 /public/ 目录
        if self._is_public_path(cos_key):
            return f"Error: /public/ is a read-only system directory"

        cos_prefix = cos_key.rstrip("/") + "/"
        entries = self._parse_dir_contents(cos_prefix)

        # 过滤掉 . 和 ..
        actual_entries = [e for e in entries if e.name not in (".", "..", ".directory")]

        if actual_entries:
            return f"Error: 目录非空: {path}"

        result = self._rm_file(cos_key.rstrip("/") + "/.directory")
        logger.info(f"[VFS] rmdir result: {result}")
        return result

    # ========== cp ==========

    def cp(self, src: str, dst: str, recursive: bool = False) -> str:
        """
        复制文件或目录

        Args:
            src: 源路径
            dst: 目标路径
            recursive: -r 递归复制目录
        """
        logger.info(f"[VFS] cp: src={src}, dst={dst}, recursive={recursive}")
        
        
        # 验证目标文件名
        import os
        dst_filename = os.path.basename(dst)
        err = self._validate_filename(dst_filename)
        if err:
            return f"Error: {err}"
        
        src_key, err = self._resolve_path(src)
        if err:
            return err

        dst_key, err = self._resolve_path(dst)
        if err:
            return err
        
        # 禁止写入 /public/ 目录
        if self._is_public_path(dst_key):
            return f"Error: /public/ is a read-only system directory"

        # 禁止从 /public/ 移出
        if self._is_public_path(src_key):
            return f"Error: /public/ files cannot be moved"

        from services.file_locker import DistributedFileLock
        locker = DistributedFileLock()
        first, second = sorted([src_key, dst_key])
        if not locker.acquire_write(self.user_id, first, f"vfs-{self.user_id}", timeout=3.0):
            return "Error: 文件被其他进程锁定，暂时无法复制"
        if not locker.acquire_write(self.user_id, second, f"vfs-{self.user_id}", timeout=3.0):
            locker.release(self.user_id, first, f"vfs-{self.user_id}")
            return "Error: 文件被其他进程锁定，暂时无法复制"
        try:
            # 检查源
            src_content = self.storage.get_file_content(src_key)
            if src_content is None:
                return f"Error: 源文件不存在: {src}"

            # 如果源是目录
            src_prefix = src_key.rstrip("/") + "/"
            src_entries = self._parse_dir_contents(src_prefix)
            is_dir = any(e.name == src.split("/")[-1] and e.type == "directory" for e in src_entries)

            if is_dir:
                if not recursive:
                    return f"Error: 是目录（使用 cp -r）: {src}"
                # 递归复制目录
                return self._cp_recursive(src_key, dst_key)

            self.storage.put_object(dst_key, src_content)

            # 异步触发向量化（非阻塞，不干扰写入流程）
            if self._should_vectorize(dst):
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(self._async_index_file(dst))
                except RuntimeError:
                    # 无事件循环（同步上下文），开新线程跑
                    import threading
                    _agent = self.agent_id or ""
                    _path = dst
                    def _run_index_cp():
                        from services.vfs_indexer import VfsIndexer
                        asyncio.run(VfsIndexer(agent_hash=_agent, file_path=_path).run())
                    threading.Thread(target=_run_index_cp, daemon=True).start()

            # 检测图片文件，提供更详细的输出
            img_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg')
            is_image = any(src.lower().endswith(ext) for ext in img_extensions)
            if is_image:
                size_kb = len(src_content) / 1024
                return f"OK: 已复制图片 ({size_kb:.1f}KB) {src} -> {dst}"
            return f"OK: 已复制 {src} -> {dst}"
        except Exception as e:
            return f"Error: cp 失败: {e}"
        finally:
            locker.release(self.user_id, second, f"vfs-{self.user_id}")
            locker.release(self.user_id, first, f"vfs-{self.user_id}")

    def _cp_recursive(self, src_prefix: str, dst_prefix: str) -> str:
        """递归复制目录"""
        try:
            objects = self._list_cos_dir(src_prefix)
            for obj in objects:
                rel_path = obj["Key"][len(src_prefix):]
                dst_key = dst_prefix.rstrip("/") + "/" + rel_path
                content = self.storage.get_file_content(obj["Key"])
                if content:
                    self.storage.put_object(dst_key, content)
            return f"OK: 已递归复制 {src_prefix} -> {dst_prefix}"
        except Exception as e:
            return f"Error: cp -r 失败: {e}"

    # ========== mv ==========

    def mv(self, src: str, dst: str) -> str:
        """移动或重命名文件/目录"""
        logger.info(f"[VFS] mv: src={src}, dst={dst}")
        
        # 禁止移动到 /public/ 目录
        dst_key, err = self._resolve_path(dst)
        if err:
            return err
        if self._is_public_path(dst_key):
            return f"Error: /public/ is a read-only system directory"

        # 禁止从 /public/ 移出
        src_key, err = self._resolve_path(src)
        if err:
            return err
        if self._is_public_path(src_key):
            return f"Error: /public/ files cannot be moved"

        # 验证目标文件名
        import os
        dst_filename = os.path.basename(dst)
        err = self._validate_filename(dst_filename)
        if err:
            return f"Error: {err}"

        from services.file_locker import DistributedFileLock
        locker = DistributedFileLock()
        first, second = sorted([src_key, dst_key])
        if not locker.acquire_write(self.user_id, first, f"vfs-{self.user_id}", timeout=3.0):
            return "Error: 文件被其他进程锁定，暂时无法移动"
        if not locker.acquire_write(self.user_id, second, f"vfs-{self.user_id}", timeout=3.0):
            locker.release(self.user_id, first, f"vfs-{self.user_id}")
            return "Error: 文件被其他进程锁定，暂时无法移动"
        try:
            # 读取源内容
            src_content = self.storage.get_file_content(src_key)
            if src_content is None:
                return f"Error: 源文件不存在: {src}"

            # 写入目标
            self.storage.put_object(dst_key, src_content)
            # 删除源
            self.storage.delete_file_by_key(src_key)
            return f"OK: 已移动 {src} -> {dst}"
        except Exception as e:
            return f"Error: mv 失败: {e}"
        finally:
            locker.release(self.user_id, second, f"vfs-{self.user_id}")
            locker.release(self.user_id, first, f"vfs-{self.user_id}")

    # ========== sort ==========

    def sort(self, path: str, reverse: bool = False, unique: bool = False) -> str:
        """
        排序文件内容

        Args:
            path: 文件路径
            reverse: -r 反向排序
            unique: -u 去重
        """
        logger.info(f"[VFS] sort: path={path}, reverse={reverse}, unique={unique}")
        content = self._read_file(path)
        if content.startswith("Error:"):
            return content

        lines = content.split("\n")
        if unique:
            seen = set()
            result_lines = []
            for line in lines:
                if line not in seen:
                    seen.add(line)
                    result_lines.append(line)
            lines = result_lines
        else:
            lines = sorted(lines, reverse=reverse)

        result = "\n".join(lines)
        logger.info(f"[VFS] sort result: {result[:300] if len(result) > 300 else result}")
        return result

    # ========== uniq ==========

    def uniq(self, path: str, count: bool = False) -> str:
        """
        去重（相邻行去重）

        Args:
            path: 文件路径
            count: -c 显示重复次数
        """
        logger.info(f"[VFS] uniq: path={path}, count={count}")
        content = self._read_file(path)
        if content.startswith("Error:"):
            return content

        lines = content.split("\n")
        if not lines:
            return ""

        result_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            count_val = 1
            while i + count_val < len(lines) and lines[i + count_val] == line:
                count_val += 1
            if count:
                result_lines.append(f"{count_val} {line}")
            else:
                result_lines.append(line)
            i += count_val

        result = "\n".join(result_lines)
        logger.info(f"[VFS] uniq result: {result[:300] if len(result) > 300 else result}")
        return result

    # ========== wc ==========

    def wc(self, path: str, mode: str = "lwc") -> str:
        """
        统计行数/字数/字节数

        Args:
            path: 文件路径
            mode: l=行数, w=字数, c=字节数, 或组合
        """
        logger.info(f"[VFS] wc: path={path}, mode={mode}")
        content = self._read_file(path)
        if content.startswith("Error:"):
            return content

        lines = content.split("\n")
        words = content.split()
        bytes_count = len(content.encode("utf-8"))

        name = path.split("/")[-1]
        results = []

        if "l" in mode:
            results.append(f"{len(lines)}")
        if "w" in mode:
            results.append(f"{len(words)}")
        if "c" in mode:
            results.append(f"{bytes_count}")

        if len(mode) > 1:
            results.append(name)

        result = " ".join(results)
        logger.info(f"[VFS] wc result: {result}")
        return result

    # ========== stat ==========

    def stat(self, path: str) -> str:
        """显示文件/目录详细信息"""
        logger.info(f"[VFS] stat: path={path}")
        cos_key, err = self._resolve_path(path)
        if err:
            return err

        # 获取父目录内容
        parent_prefix = cos_key.rsplit("/", 1)[0] + "/" if "/" in cos_key else self.base_path
        entries = self._parse_dir_contents(parent_prefix)

        name = cos_key.rstrip("/").split("/")[-1]
        entry = next((e for e in entries if e.name == name), None)

        if entry is None:
            # 可能是目录
            cos_prefix = cos_key.rstrip("/") + "/"
            entries = self._parse_dir_contents(cos_prefix)
            for e in entries:
                if e.name == name and e.type == "directory":
                    entry = e
                    break

        if entry is None:
            return f"Error: 文件不存在: {path}"

        # 获取完整信息
        if entry.type == "directory":
            self._update_dir_nlink(entry, parent_prefix)

        dt = datetime.fromtimestamp(entry.mtime)

        lines = [
            f"  File: {entry.name}",
            f"  Size: {entry.size}       Blocks: {entry.size // 512 + 1}   IO Block: 4096   {entry.type}",
            f"Device: {entry.inode:>8}      Inode: {entry.inode:>8}  Links: {entry.nlink}",
            f"Access: ({self._mode_to_octal(entry.mode)}/{self._mode_to_perm_string(entry.mode)})  Uid: (1000/user)   Gid: (1000/user)",
            f"Access: {dt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Modify: {dt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Change: {dt.strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        result = "\n".join(lines)
        logger.info(f"[VFS] stat result: {result}")
        return result

    def _mode_to_octal(self, mode: str) -> str:
        """权限字符串转八进制"""
        return _mode_to_octal_impl(mode)

    def _mode_to_perm_string(self, mode: str) -> str:
        """权限字符串转 rwx 格式"""
        return _mode_to_perm_string_impl(mode)

    # ========== 主入口 ==========

    def execute(self, command: str) -> str:
        """
        解析并执行 bash 命令，返回字符串输出

        支持的命令：
        - ls [flags] [path]
        - cd [path]
        - pwd
        - cat <file>...
        - head [-n N] <file>
        - tail [-n N] <file>
        - echo <text> [> | >>] <file>
        - grep [flags] <pattern> <file>
        - find [path] [-name pattern] [-type d|f]
        - touch <file>
        - mkdir [-p] <dir>
        - rm [-rf] <file>
        - rmdir <dir>
        - cp [-r] <src> <dst>
        - mv <src> <dst>
        - wc [-l|-c|-w] <file>
        - stat <file>
        - sort [-r] [-u] <file>
        - uniq [-c] <file>
        - date [+format]
        - cut [-d delim] [-f fields] <file>
        - diff <file1> <file2>
        - awk <pattern|action> <file>
        - sed <pattern|action> <file>
        - <cmd1> | <cmd2>  管道操作
        - <cmd1> && <cmd2>  条件执行
        - <cmd1> || <cmd2>  条件执行
        - <cmd1> ; <cmd2>  顺序执行
        """
        command = command.strip()
        if not command:
            return ""

        logger.info(f"[VFS] execute: command={command}, cwd={self._cwd}")

        # python3 管道检测：管道到 python3 暂不支持
        if "|" in command and "python3" in command:
            return "Error: 管道到 python3 暂不支持，请直接使用 python3 -c 执行"

        try:
            # Tokenize
            tokenizer = Tokenizer(command)
            tokens = tokenizer.tokenize()

            # Parse
            parser = Parser(tokens)
            ast = parser.parse()

            # Execute via executor
            executor = Executor(self, self._registry)
            result = executor.execute_sequence(ast)

            # Combine stdout and stderr for backward compatibility
            return result.combine()
        except Exception as e:
            logger.error(f"[VFS] execute error: {e}")
            return f"Error: {str(e)}"

    # Keep legacy methods for backward compatibility during transition
    # These are called by the old execute() implementation
    def _execute_ls(self, command: str) -> str:
        """解析并执行 ls 命令"""
        if command == "ls":
            return self.ls()

        # 创建临时文件用于传递数据
        temp_files = []
        for i in range(len(segments) - 1):
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=f".pipe{i}")
            tf.close()
            temp_files.append(tf.name)

        # 需要直接操作文件的 filter 命令（不使用 VFS execute）
        filter_commands = {"grep", "head", "tail", "sort", "uniq", "wc", "tr", "sed", "awk"}

        try:
            prev_output = None
            for i, seg in enumerate(segments):
                seg = seg.strip()
                if not seg:
                    continue

                parts = seg.split()
                cmd = parts[0] if parts else ""

                if i == 0:
                    # 第一个命令：直接执行，结果传给下一个
                    output = self.execute(seg)
                    if i < len(temp_files):
                        with open(temp_files[i], "w", encoding="utf-8") as f:
                            f.write(output)
                    prev_output = output
                elif i < len(temp_files) - 1:
                    # 中间命令：需要注入前一个的输出作为输入
                    if prev_output:
                        with open(temp_files[i-1], "w", encoding="utf-8") as f:
                            f.write(prev_output)

                    # 对于 filter 命令，直接读取临时文件并处理
                    if cmd in filter_commands:
                        output = self._execute_filter_command(seg, temp_files[i-1], temp_files[i])
                    else:
                        # 其他命令通过 execute（不应该走到这里，因为 filter_commands 已覆盖大多数管道命令）
                        output = self.execute(seg)
                    prev_output = output
                else:
                    # 最后一个命令
                    with open(temp_files[i-1], "w", encoding="utf-8") as f:
                        f.write(prev_output if prev_output else "")

                    if cmd in filter_commands:
                        output = self._execute_filter_command(seg, temp_files[i-1], None)
                    else:
                        output = self.execute(seg)
                    return output

            return prev_output if prev_output else ""
        finally:
            # 清理临时文件
            for tf in temp_files:
                try:
                    os.unlink(tf)
                except Exception:
                    pass

    def _execute_filter_command(self, command: str, input_file: str, output_file: Optional[str]) -> str:
        """
        直接执行 filter 命令（grep, wc, tr, sort, uniq, head, tail 等）

        Args:
            command: 完整命令字符串
            input_file: 输入临时文件路径（绝对路径）
            output_file: 输出临时文件路径（绝对路径），None 表示输出到结果字符串

        Returns:
            输出字符串（如果 output_file 为 None），否则返回空字符串
        """
        parts = command.split()
        if not parts:
            return ""

        cmd = parts[0]
        args = parts[1:]

        # 读取输入文件
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return f"Error: 读取输入失败: {e}"

        result = ""

        if cmd == "grep":
            result = self._filter_grep(args, content)
        elif cmd == "wc":
            result = self._filter_wc(args, content)
        elif cmd == "tr":
            result = self._filter_tr(args, content)
        elif cmd == "sort":
            result = self._filter_sort(args, content)
        elif cmd == "uniq":
            result = self._filter_uniq(args, content)
        elif cmd == "head":
            result = self._filter_head(args, content)
        elif cmd == "tail":
            result = self._filter_tail(args, content)
        elif cmd == "sed":
            result = self._filter_sed(args, content)
        elif cmd == "awk":
            result = self._filter_awk(args, content)
        else:
            return f"Error: 不支持的 filter 命令: {cmd}"

        # 写入输出文件或返回结果
        if output_file:
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(result)
                return ""
            except Exception as e:
                return f"Error: 写入输出失败: {e}"
        return result

    def _filter_grep(self, args: List[str], content: str) -> str:
        """grep 过滤器"""
        ignore_case = False
        invert = False
        only_matching = False  # -o 选项
        pattern = ""

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-i":
                ignore_case = True
            elif p == "-v":
                invert = True
            elif p == "-o":
                only_matching = True
            elif not p.startswith("-"):
                pattern = p.strip("\"'")
            i += 1

        if not pattern:
            return content

        lines = content.split("\n")
        matching = []
        for line in lines:
            test_line = line if not ignore_case else line.lower()
            test_pattern = pattern if not ignore_case else pattern.lower()
            
            if only_matching:
                # -o: 输出匹配的部分，每个匹配一行
                found_count = test_line.count(test_pattern)
                if found_count > 0:
                    if invert:
                        # -v: 输出不包含匹配的行（罕见组合，输出整行）
                        matching.append(line)
                    else:
                        # 每个匹配输出一行
                        for _ in range(found_count):
                            matching.append(test_pattern)
            else:
                # 普通模式：输出匹配的行
                found = test_pattern in test_line
                if invert:
                    found = not found
                if found:
                    matching.append(line)

        return "\n".join(matching)

    def _filter_wc(self, args: List[str], content: str) -> str:
        """wc 过滤器"""
        mode = "lwc"

        for p in args:
            if p.startswith("-"):
                mode = p[1:]

        lines = content.split("\n")
        words = content.split()
        bytes_count = len(content.encode("utf-8"))

        results = []
        if "l" in mode:
            results.append(str(len(lines)))
        if "w" in mode:
            results.append(str(len(words)))
        if "c" in mode:
            results.append(str(bytes_count))

        return " ".join(results)

    def _filter_tr(self, args: List[str], content: str) -> str:
        """tr 过滤器"""
        if len(args) < 2:
            return content

        set1 = args[0].strip("'")
        set2 = args[1].strip("'") if len(args) > 1 else ""

        # 简单实现：字符替换
        result = content
        for i, c in enumerate(set1):
            if i < len(set2):
                result = result.replace(c, set2[i])
            else:
                # 如果 set2 短于 set1，最后一个字符重复
                result = result.replace(c, set2[-1] if set2 else "")

        return result

    def _filter_sort(self, args: List[str], content: str) -> str:
        """sort 过滤器"""
        reverse = False
        unique = False

        for p in args:
            if p == "-r":
                reverse = True
            elif p == "-u":
                unique = True

        lines = content.split("\n")
        if unique:
            seen = set()
            result_lines = []
            for line in lines:
                if line not in seen:
                    seen.add(line)
                    result_lines.append(line)
            lines = result_lines
        else:
            lines = sorted(lines, reverse=reverse)

        return "\n".join(lines)

    def _filter_uniq(self, args: List[str], content: str) -> str:
        """uniq 过滤器"""
        count = False

        for p in args:
            if p == "-c":
                count = True

        lines = content.split("\n")
        if not lines:
            return ""

        result_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            count_val = 1
            while i + count_val < len(lines) and lines[i + count_val] == line:
                count_val += 1
            if count:
                result_lines.append(f"{count_val} {line}")
            else:
                result_lines.append(line)
            i += count_val

        return "\n".join(result_lines)

    def _filter_head(self, args: List[str], content: str) -> str:
        """head 过滤器"""
        n = 10

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-n" and i + 1 < len(args):
                try:
                    n = int(args[i + 1])
                except ValueError:
                    pass
                i += 2
            elif p.startswith("-n") and len(p) > 2:
                try:
                    n = int(p[2:])
                except ValueError:
                    pass
                i += 1
            elif p.isdigit():
                try:
                    n = int(p)
                except ValueError:
                    pass
                i += 1
            else:
                i += 1

        lines = content.split("\n")
        return "\n".join(lines[:n])

    def _filter_tail(self, args: List[str], content: str) -> str:
        """tail 过滤器"""
        n = 10

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-n" and i + 1 < len(args):
                try:
                    n = int(args[i + 1])
                except ValueError:
                    pass
                i += 2
            elif p.startswith("-n") and len(p) > 2:
                try:
                    n = int(p[2:])
                except ValueError:
                    pass
                i += 1
            elif p.isdigit():
                try:
                    n = int(p)
                except ValueError:
                    pass
                i += 1
            else:
                i += 1

        lines = content.split("\n")
        return "\n".join(lines[-n:])

    def _filter_sed(self, args: List[str], content: str) -> str:
        """sed 过滤器（简化实现）"""
        # 支持 s/pattern/replacement/g
        pattern = None
        replacement = ""
        global_replace = False

        for p in args:
            if p.startswith("s/"):
                parts = p[2:].rsplit("/", 2)
                if len(parts) >= 2:
                    pattern = parts[0]
                    replacement = parts[1]
                    global_replace = len(parts) > 2 and parts[2] == "g"

        if pattern is None:
            return content

        if global_replace:
            result = re.sub(pattern, replacement, content)
        else:
            result = re.sub(pattern, replacement, content, count=1)

        return result

    def _filter_awk(self, args: List[str], content: str) -> str:
        """awk 过滤器（简化实现）"""
        # 支持 '{print $1}' 或 '{print $NF}' 等
        field_spec = None

        for p in args:
            if p.startswith("'"):
                inner = p.strip("'")
                if "{print" in inner and "$" in inner:
                    # 提取字段号，如 $1, $NF
                    match = re.search(r'\$(\d+|NF)', inner)
                    if match:
                        field_str = match.group(1)
                        field_spec = int(field_str) if field_str.isdigit() else field_str

        if field_spec is None:
            return content

        lines = content.split("\n")
        result_lines = []
        for line in lines:
            fields = line.split()
            if field_spec == "NF":
                if fields:
                    result_lines.append(fields[-1])
            elif 0 < field_spec <= len(fields):
                result_lines.append(fields[field_spec - 1])

        return "\n".join(result_lines)

    def _execute_ls(self, command: str) -> str:
        """解析并执行 ls 命令"""
        if command == "ls":
            return self.ls()

        # 去掉 ls 前缀
        rest = command[3:].strip()
        parts = rest.split()

        # flags
        show_all = False
        long_format = False
        human = False
        recursive = False
        classify = False
        sort_by = "name"
        reverse = False
        directory_only = False
        show_inode = False
        oneline = False
        path = ""

        i = 0
        while i < len(parts):
            p = parts[i]
            if p in ("-a", "--all"):
                show_all = True
            elif p in ("-A", "--almost-all"):
                show_all = True  # 简化处理
            elif p in ("-l", "--long"):
                long_format = True
            elif p in ("-h", "--human-readable"):
                human = True
            elif p in ("-R", "--recursive"):
                recursive = True
            elif p in ("-F", "--classify"):
                classify = True
            elif p in ("-t",):
                sort_by = "time"
            elif p in ("-S",):
                sort_by = "size"
            elif p in ("-r", "--reverse"):
                reverse = True
            elif p in ("-d", "--directory"):
                directory_only = True
            elif p in ("-1",):
                oneline = True
            elif p in ("-i", "--inode"):
                show_inode = True
            elif p.startswith("-") and len(p) > 1:
                # 组合 flag，如 -la
                for c in p[1:]:
                    if c == "a":
                        show_all = True
                    elif c == "l":
                        long_format = True
                    elif c == "h":
                        human = True
                    elif c == "R":
                        recursive = True
                    elif c == "F":
                        classify = True
                    elif c == "t":
                        sort_by = "time"
                    elif c == "S":
                        sort_by = "size"
                    elif c == "r":
                        reverse = True
                    elif c == "d":
                        directory_only = True
                    elif c == "1":
                        oneline = True
                    elif c == "i":
                        show_inode = True
            else:
                path = p
            i += 1

        return self.ls(path, show_all, long_format, human, recursive, classify,
                      sort_by, reverse, directory_only, show_inode, oneline)

    def _execute_echo(self, command: str) -> str:
        """解析 echo 命令"""
        # echo "text" > file
        match = re.match(r'^echo\s+"(.*)"\s+>>\s+(.+)$', command)
        if match:
            text, path = match.groups()
            return self.echo(text, path.strip(), append=True)

        match = re.match(r'^echo\s+"(.*)"\s+>\s+(.+)$', command)
        if match:
            text, path = match.groups()
            return self.echo(text, path.strip(), append=False)

        # echo 'text' > file
        match = re.match(r"^echo\s+'(.*)'\s+>>\s+(.+)$", command)
        if match:
            text, path = match.groups()
            return self.echo(text, path.strip(), append=True)

        match = re.match(r"^echo\s+'(.*)'\s+>\s+(.+)$", command)
        if match:
            text, path = match.groups()
            return self.echo(text, path.strip(), append=False)

        # echo text > file (无引号)
        match = re.match(r"^echo\s+(\S+)\s+>>\s+(.+)$", command)
        if match:
            text, path = match.groups()
            return self.echo(text, path.strip(), append=True)

        match = re.match(r"^echo\s+(\S+)\s+>\s+(.+)$", command)
        if match:
            text, path = match.groups()
            return self.echo(text, path.strip(), append=False)

        # echo text（无重定向）
        match = re.match(r"^echo\s+(.+)$", command)
        if match:
            text = match.group(1).strip('"').strip("'")
            return text

        return f"Error: echo 用法: echo \"text\" > file 或 echo \"text\" >> file"

    def _execute_grep(self, command: str) -> str:
        """解析 grep 命令"""
        # grep pattern file
        # grep -i pattern file
        # grep -n pattern file
        # grep -v pattern file
        # grep -r pattern dir

        parts = command[5:].strip().split()
        if len(parts) < 2:
            return "Error: grep 用法: grep <pattern> <文件>"

        ignore_case = False
        show_line_no = True
        invert = False
        recursive = False
        name_only = False
        only_matching = False

        pattern = ""
        paths = []

        i = 0
        while i < len(parts):
            p = parts[i]
            if p == "-i":
                ignore_case = True
            elif p == "-n":
                show_line_no = True
            elif p == "-v":
                invert = True
            elif p == "-r":
                recursive = True
            elif p == "-l":
                name_only = True
            elif p == "-o":
                only_matching = True
            elif p.startswith("-"):
                pass  # 忽略其他 flag
            elif p in ("|", ">", ">>", "2>", "2>&1"):
                pass  # 忽略 shell 操作符
            elif not pattern:
                pattern = p.strip("\"'")
            else:
                paths.append(p)
            i += 1

        if not pattern:
            return "Error: grep 用法: grep <pattern> <文件>"
        if not paths:
            return "Error: grep 用法: grep <pattern> <文件>"

        return self.grep(pattern, *paths, ignore_case=ignore_case,
                        show_line_no=show_line_no, invert=invert,
                        recursive=recursive, name_only=name_only,
                        only_matching=only_matching)

    def _execute_find(self, command: str) -> str:
        """解析 find 命令"""
        parts = command[5:].strip().split()
        if not parts:
            return self.find()

        path = "."
        name_pattern = ""
        find_type = ""

        i = 0
        while i < len(parts):
            p = parts[i]
            if p == "-name" and i + 1 < len(parts):
                name_pattern = parts[i + 1].strip('"').strip("'")
                i += 2
            elif p == "-type" and i + 1 < len(parts):
                find_type = parts[i + 1]
                i += 2
            elif not p.startswith("-"):
                path = p
                i += 1
            else:
                i += 1

        return self.find(path, name_pattern, find_type)

    # ========== date ==========

    def _execute_date(self, command: str) -> str:
        """解析并执行 date 命令"""
        # date
        # date +%Y
        # date +%m
        # date +%d
        # date +%H
        # date +%M
        # date +%S
        # date +%Y-%m-%d
        # etc.

        parts = command[4:].strip()
        fmt = parts.strip() if parts else ""

        now = datetime.now()

        if not fmt:
            # 默认格式
            return now.strftime("%a %b %d %H:%M:%S %Y")

        # 移除 + 前缀（如果有）
        fmt = fmt.lstrip("+")

        # 已经是标准 strftime 格式，直接使用
        # 用户可以使用 %Y, %m, %d, %H, %M, %S 等标准格式
        try:
            return now.strftime(fmt)
        except Exception:
            return now.strftime("%a %b %d %H:%M:%S %Y")

    # ========== cut ==========

    def _execute_cut(self, command: str) -> str:
        """解析并执行 cut 命令"""
        # cut -d: -f1 file
        # cut -c1-10 file
        # cut -c1 file
        # cut -f1 file

        parts = command[4:].strip().split()
        if not parts:
            return "Error: 用法: cut [-d delim] [-f fields] [-c fields] <文件>"

        delimiter = "\t"
        fields = None
        chars = None
        path = ""

        i = 0
        while i < len(parts):
            p = parts[i]
            if p == "-d" and i + 1 < len(parts):
                delimiter = parts[i + 1]
                i += 2
            elif p == "-f" and i + 1 < len(parts):
                fields_str = parts[i + 1]
                fields = self._parse_cut_fields(fields_str)
                i += 2
            elif p == "-c" and i + 1 < len(parts):
                chars_str = parts[i + 1]
                chars = self._parse_cut_fields(chars_str)
                i += 2
            elif p.startswith("-d"):
                delimiter = p[2:]
                i += 1
            elif p.startswith("-f"):
                fields_str = p[2:]
                fields = self._parse_cut_fields(fields_str)
                i += 1
            elif p.startswith("-c"):
                chars_str = p[2:]
                chars = self._parse_cut_fields(chars_str)
                i += 1
            else:
                path = p
                i += 1

        if not path:
            return "Error: 用法: cut [-d delim] [-f fields] [-c fields] <文件>"

        content = self._read_file(path)
        if content.startswith("Error:"):
            return content

        lines = content.split("\n")
        result_lines = []

        if chars is not None:
            # 字符模式
            for line in lines:
                result = self._cut_by_chars(line, chars)
                if result:
                    result_lines.append(result)
        elif fields is not None:
            # 字段模式
            for line in lines:
                result = self._cut_by_fields(line, fields, delimiter)
                if result:
                    result_lines.append(result)
        else:
            return "Error: 用法: cut 必须指定 -f 或 -c"

        return "\n".join(result_lines)

    def _parse_cut_fields(self, fields_str: str) -> List[Tuple[int, int]]:
        """解析字段规格，如 '1', '1-3', '1,3,5'"""
        return _parse_cut_fields_impl(fields_str)

    def _cut_by_fields(self, line: str, fields: List[Tuple[int, int]], delimiter: str) -> str:
        """按字段剪切"""
        parts = line.split(delimiter)
        result = []
        for start, end in fields:
            if end == -1:
                end = len(parts)
            for i in range(start, end + 1):
                if 0 < i <= len(parts):
                    result.append(parts[i - 1])
        return delimiter.join(result)

    def _cut_by_chars(self, line: str, chars: List[Tuple[int, int]]) -> str:
        """按字符剪切"""
        result = []
        for start, end in chars:
            if end == -1:
                end = len(line)
            for i in range(start, end + 1):
                if 0 < i <= len(line):
                    result.append(line[i - 1])
        return "".join(result)

    # ========== diff ==========

    def _execute_diff(self, command: str) -> str:
        """解析并执行 diff 命令"""
        # diff file1 file2

        parts = command[5:].strip().split()
        if len(parts) < 2:
            return "Error: 用法: diff <文件1> <文件2>"

        file1 = parts[0]
        file2 = parts[1]

        content1 = self._read_file(file1)
        if content1.startswith("Error:"):
            return content1

        content2 = self._read_file(file2)
        if content2.startswith("Error:"):
            return content2

        lines1 = content1.split("\n")
        lines2 = content2.split("\n")

        # 简单的行比较
        i = 0
        j = 0
        result = []

        # 找到第一个差异
        first_diff = None
        while i < len(lines1) and j < len(lines2):
            if lines1[i] != lines2[j]:
                first_diff = (i, j)
                break
            i += 1
            j += 1

        # 处理末尾差异
        if first_diff is None:
            if len(lines1) != len(lines2):
                first_diff = (i, j)

        if first_diff is None:
            # 相同
            return ""

        # 输出差异
        start1, start2 = first_diff

        # 显示后续相关行
        i = start1
        j = start2

        while i < len(lines1) or j < len(lines2):
            line1 = lines1[i] if i < len(lines1) else None
            line2 = lines2[j] if j < len(lines2) else None

            if line1 is not None and line2 is not None and line1 == line2:
                # 相同的行
                result.append(f"  {line1}")
                i += 1
                j += 1
            elif line1 is None:
                # file1 结束
                result.append(f"> {line2}")
                j += 1
            elif line2 is None:
                # file2 结束
                result.append(f"< {line1}")
                i += 1
            else:
                # 不同
                result.append(f"< {line1}")
                result.append(f"> {line2}")
                i += 1
                j += 1

            # 限制输出行数
            if len(result) > 100:
                result.append("... (diff truncated)")
                break

        return "\n".join(result)

    # ========== awk ==========

    def _execute_awk(self, command: str) -> str:
        """解析并执行 awk 命令"""
        # awk "pattern" file
        # awk "{print $1}" file
        # awk -F":" "{print $1}" file
        # awk "NR==1" file
        # awk "END{...}" file

        # 直接从原始命令中解析，保留引号内的完整内容
        cmd_rest = command[4:].strip()  # 去掉 "awk "

        field_sep = None
        file_path = ""
        pattern = ""

        # 查找 -F 参数
        fs_match = re.search(r'-F(\S+)|-F\s+(\S+)', cmd_rest)
        if fs_match:
            field_sep = fs_match.group(1) or fs_match.group(2)
            cmd_rest = cmd_rest.replace(fs_match.group(0), "").strip()

        # 查找引号内的内容作为 pattern/action
        quote_match = re.search(r'''(['"])(.+?)\1''', cmd_rest)
        if quote_match:
            pattern = quote_match.group(2)
            # 移除引号部分，剩下的是文件路径
            after_quote = cmd_rest[quote_match.end():].strip()
            if after_quote:
                file_path = after_quote.split()[0] if after_quote.split() else ""
        else:
            # 没有引号，按空格分割
            parts = cmd_rest.split()
            if len(parts) >= 2:
                pattern = parts[0]
                file_path = parts[1]
            elif len(parts) == 1:
                pattern = parts[0]

        if not file_path:
            return "Error: awk 用法: awk <pattern|action> <文件>"

        content = self._read_file(file_path)
        if content.startswith("Error:"):
            return content

        return self._awk_process(content, pattern, field_sep)

    def _awk_process(self, content: str, pattern: str, field_sep: Optional[str] = None) -> str:
        """处理 awk 逻辑"""
        lines = content.split("\n")
        sep = field_sep if field_sep else None
        result_lines = []
        end_action_result = ""

        # 判断是 pattern 还是 action
        is_end_block = "END" in pattern.upper()
        has_action = "{" in pattern and "}" in pattern

        if is_end_block and has_action:
            # 处理 END 块
            body_pattern = ""
            end_expr = ""

            # 提取 END 之前的部分作为 body pattern 和 END 块
            if "END" in pattern.upper():
                # 简化处理：假设 END 是整个表达式
                end_expr = pattern

            # 先处理所有行
            matched_lines = []
            for line in lines:
                if self._awk_match(line, body_pattern, sep):
                    matched_lines.append(line)

            # 计算 END 表达式的结果
            if "sum" in end_expr.lower() or "total" in end_expr.lower():
                # 尝试计算总和
                total = 0
                for line in matched_lines:
                    fields = line.split(sep) if sep else line.split()
                    if fields:
                        try:
                            total += float(fields[0])
                        except (ValueError, IndexError):
                            pass
                end_action_result = str(int(total) if total == int(total) else total)

            elif "count" in end_expr.lower() or "nr" in end_expr.lower():
                end_action_result = str(len(matched_lines))

            elif "print" in end_expr.lower():
                # 简单 print NR 或类似
                if "NR" in end_expr:
                    end_action_result = str(len(lines))

        elif has_action:
            # action 块，如 {print $1}
            for line in lines:
                result = self._awk_action(line, pattern, sep)
                if result is not None:
                    result_lines.append(result)
        elif pattern:
            # 只有 pattern，打印匹配的行
            for line in lines:
                if self._awk_match(line, pattern, sep):
                    result_lines.append(line)

        if end_action_result:
            return end_action_result

        return "\n".join(result_lines)

    def _awk_match(self, line: str, pattern: str, field_sep: Optional[str] = None) -> bool:
        """检查行是否匹配 awk pattern"""
        if not pattern:
            return True

        # NR==N 模式（行号匹配）
        nr_match = re.match(r'NR\s*==\s*(\d+)', pattern.strip())
        if nr_match:
            # 需要上下文，这里无法知道当前行号
            return False

        # 简单的字符串包含匹配
        test_line = line
        test_pattern = pattern.strip('"\'')
        return test_pattern in test_line

    def _awk_action(self, line: str, action: str, field_sep: Optional[str] = None) -> Optional[str]:
        """执行 awk action"""
        # 提取 {print ...}
        match = re.search(r'\{(.+)\}', action)
        if not match:
            return line

        expr = match.group(1).strip()

        if expr.startswith("print"):
            print_expr = expr[5:].strip()

            # print $1, $2, etc.
            fields = line.split(field_sep) if field_sep else line.split()

            if not print_expr:
                return line

            # 处理 print $N 或 print $NF
            result_parts = []
            i = 0
            while i < len(print_expr):
                if print_expr[i] == '$':
                    # 提取字段号
                    j = i + 1
                    while j < len(print_expr) and print_expr[j].isdigit():
                        j += 1
                    if j > i + 1:
                        field_num = int(print_expr[i+1:j])
                        if 0 < field_num <= len(fields):
                            result_parts.append(fields[field_num - 1])
                        i = j
                    elif j < len(print_expr) and print_expr[j] == 'N' and j + 1 < len(print_expr) and print_expr[j+1] == 'F':
                        # $NF
                        if fields:
                            result_parts.append(fields[-1])
                        i = j + 2
                    else:
                        i += 1
                else:
                    i += 1

            if result_parts:
                return " ".join(result_parts)
            return line

        return line

    # ========== sed ==========

    def _execute_sed(self, command: str) -> str:
        """解析并执行 sed 命令"""
        # sed "s/old/new/g" file
        # sed "s/old/new/" file
        # sed "Nd" file
        # sed "/pattern/d" file

        # 直接从原始命令中解析
        cmd_rest = command[4:].strip()  # 去掉 "sed "

        file_path = ""
        script = ""

        # 查找引号内的内容作为 script
        quote_match = re.search(r'''(['"])(.+?)\1''', cmd_rest)
        if quote_match:
            script = quote_match.group(2)
            # 移除引号部分，剩下的是文件路径
            after_quote = cmd_rest[quote_match.end():].strip()
            if after_quote:
                file_path = after_quote.split()[0] if after_quote.split() else ""
        else:
            # 没有引号，按空格分割
            parts = cmd_rest.split()
            if len(parts) >= 2:
                script = parts[0]
                file_path = parts[1]
            elif len(parts) == 1:
                script = parts[0]

        if not file_path:
            return "Error: sed 用法: sed <pattern|action> <文件>"

        content = self._read_file(file_path)
        if content.startswith("Error:"):
            return content

        return self._sed_process(content, script)

    def _sed_process(self, content: str, script: str) -> str:
        """处理 sed 逻辑"""
        lines = content.split("\n")
        result_lines = []

        # s/old/new/ - 替换
        if script.startswith("s/"):
            parts = script[2:].rsplit("/", 2)
            if len(parts) >= 2:
                old = parts[0]
                new = parts[1]
                global_replace = len(parts) > 2 and parts[2] == "g"

                for line in lines:
                    if global_replace:
                        result_lines.append(re.sub(old, new, line))
                    else:
                        result_lines.append(re.sub(old, new, line, count=1))
                return "\n".join(result_lines)

        # Nd - 删除第 N 行
        nd_match = re.match(r'(\d+)d', script)
        if nd_match:
            line_num = int(nd_match.group(1))
            for i, line in enumerate(lines, 1):
                if i != line_num:
                    result_lines.append(line)
            return "\n".join(result_lines)

        # /pattern/d - 删除匹配的行
        if script.startswith("/") and script.endswith("/d"):
            pattern = script[1:-2]
            for line in lines:
                if pattern not in line:
                    result_lines.append(line)
            return "\n".join(result_lines)

        # /pattern/s/old/new/ - 替换匹配行
        pattern_match = re.match(r'/(.+)/s/(.+)/(.+)/(.*)', script)
        if pattern_match:
            pat, old, new, flags = pattern_match.groups()
            global_replace = 'g' in flags
            for line in lines:
                if pat in line:
                    if global_replace:
                        result_lines.append(re.sub(old, new, line))
                    else:
                        result_lines.append(re.sub(old, new, line, count=1))
                else:
                    result_lines.append(line)
            return "\n".join(result_lines)

        return content

# ========== 模块级便捷函数 ==========

def get_virtual_file_content(vfs_path: str, user_id: Optional[str] = None) -> Optional[bytes]:
    """
    通过 VFS 路径获取文件内容（模块级便捷函数）

    Args:
        vfs_path: VFS 路径，如 "/workspace/output/test.ggb"
        user_id: 用户ID（可选，如果不提供则需要路径包含用户前缀）

    Returns:
        文件内容字节，不存在或失败返回 None
    """
    if not user_id:
        # 从路径中尝试提取 user_id
        # 路径格式: /workspace/... 或 feclaw/user_workspaces/{user_id}/workspace/...
        return None

    try:
        vfs = VirtualFileSystem(user_id)
        key, err = vfs._resolve_path(vfs_path)
        if err:
            return None

        content = vfs.storage.get_file_content(key)
        return content
    except Exception:
        return None
