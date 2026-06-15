"""
VFS FUSE 守护进程 - 将 VirtualFileSystem 挂载为本地目录

通过 pyfuse3 将基于腾讯云 COS 的虚拟文件系统暴露为本地 FUSE 挂载点，
使所有程序（cat、ls、vim、python、node 等）都能透明访问。

所有 FUSE 回调均为 async def（pyfuse3 要求）。
"""
from __future__ import annotations

import os
import stat
import errno
import logging
from typing import Dict, Optional, Tuple

# pyfuse3 延迟导入：允许 check_fuse_available() 在 import 失败时返回 False
try:
    import pyfuse3
    _PYFUSE3_AVAILABLE = True
except ImportError:
    _PYFUSE3_AVAILABLE = False
    pyfuse3 = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# 根目录 inode
ROOT_INODE = 1
# 文件句柄起始值
FH_START = 2


def check_fuse_available() -> bool:
    """检查 FUSE 是否可用"""
    import shutil
    try:
        if not _PYFUSE3_AVAILABLE:
            return False
        if not os.path.exists("/dev/fuse"):
            return False
        if shutil.which("fusermount3") is None:
            return False
        return True
    except Exception:
        return False


def _make_attr(inode: int, entry_type: str, size: int, mtime: float,
               nlink: int, uid: int, gid: int, cache_ttl: float) -> pyfuse3.EntryAttributes:
    """创建 pyfuse3 EntryAttributes"""
    attr = pyfuse3.EntryAttributes()
    attr.st_ino = inode
    attr.generation = 0
    attr.attr_timeout = cache_ttl
    attr.entry_timeout = cache_ttl
    if entry_type == "directory":
        attr.st_mode = stat.S_IFDIR | 0o755
        attr.st_nlink = max(nlink, 2)
    else:
        attr.st_mode = stat.S_IFREG | 0o644
        attr.st_nlink = nlink
    attr.st_uid = uid
    attr.st_gid = gid
    attr.st_size = size
    # pyfuse3 3.4.x 使用纳秒精度时间戳
    attr.st_atime_ns = int(mtime * 1_000_000_000)
    attr.st_mtime_ns = int(mtime * 1_000_000_000)
    attr.st_ctime_ns = int(mtime * 1_000_000_000)
    attr.st_blksize = 512
    attr.st_blocks = max((size + 511) // 512, 1)
    return attr


# FUSE 基类：pyfuse3 不可用时用 object 作为 fallback
if _PYFUSE3_AVAILABLE:
    _FUSE_BASE = pyfuse3.Operations
else:
    _FUSE_BASE = object


class VFSFuseDaemon(_FUSE_BASE):  # type: ignore[valid-type,misc]
    """
    pyfuse3 Operations 实现，将 VFS 操作代理到 VirtualFileSystem。

    所有回调方法均为 async def，路径参数为 bytes 类型。
    """

    # ── 根目录虚拟条目（硬编码） ──────────────────────────────────
    # 全局模式：完整 COS 树 + config 列表
    ROOT_DIRS_GLOBAL = ["agents", "user_workspaces", "public", "config"]
    # sandbox 模式：workspace + agent + config + public
    ROOT_DIRS_SANDBOX = ["workspace", "agent", "config", "public"]

    # ── /config/ 目录树（硬编码） ─────────────────────────────────
    _CONFIG_DIR_TREE = {
        "": {  # /config/ 根
            "dirs": ["global", "channels", "tasks", "platform"],
            "files": ["persona", "tools", "memory_sync.interval", "config"],
        },
        "global": {
            "dirs": [],
            "files": ["streaming", "show_tool_calls", "heartbeat.interval",
                       "session.auto_load", "session.summary_trigger_count", "deep_thinking"],
        },
        "channels": {
            "dirs": ["feishu", "wechat", "web"],
            "files": [],
        },
        "channels/feishu": {"dirs": [], "files": ["streaming"]},
        "channels/wechat": {"dirs": [], "files": ["streaming"]},
        "channels/web":    {"dirs": [], "files": ["streaming"]},
        "tasks": {
            "dirs": [],
            "files": ["default.output", "default.session_mode", "default.pre_generate"],
        },
        "platform": {
            "dirs": [],
            "files": ["fuse.timeout", "cleanup.interval", "health_check.interval"],
        },
    }

    # 实例级属性，在 __init__ 中设置
    ROOT_DIRS = ROOT_DIRS_GLOBAL  # 默认全局模式

    def __init__(self, vfs, mount_dir: str, cache_ttl: int = 60, cos_prefix: str = "feclaw/",
                 agent_hash: str = None):
        # ⚠️ 不调用 super().__init__() — pyfuse3.Operations 的 Cython init
        # 会导致 fuse_session_mount 挂起
        self.vfs = vfs
        self.mount_dir = mount_dir
        self.cache_ttl = cache_ttl
        self.cos_prefix = cos_prefix.rstrip("/") + "/"  # 确保以 / 结尾
        self.agent_hash = agent_hash
        self.uid = os.getuid()
        self.gid = os.getgid()

        # sandbox 模式：覆盖根目录条目列表
        if self.agent_hash:
            self.ROOT_DIRS = self.ROOT_DIRS_SANDBOX
        else:
            self.ROOT_DIRS = self.ROOT_DIRS_GLOBAL

        # inode ↔ vpath 双向映射
        self._inode_to_path: Dict[int, str] = {ROOT_INODE: "/"}
        self._path_to_inode: Dict[str, int] = {"/": ROOT_INODE}
        self._next_inode = ROOT_INODE + 1

        # 文件句柄管理: fh -> {inode, path, content}
        self._open_files: Dict[int, dict] = {}
        # 目录句柄管理: fh -> [(name, attr, entry_id), ...]
        self._dir_handles: Dict[int, list] = {}
        self._next_fh = FH_START

        # 并发保护
        import threading
        self._inode_lock = threading.RLock()  # 保护 inode 映射写操作（trio 单线程，RLock 安全；_ensure_inode 可能递归调用 _get_inode）
        self._file_locks: Dict[str, trio.Lock] = {}  # per-file RMW 锁

    # ── COS 路径转换辅助方法 ─────────────────────────────────────

    def _cos_prefix_for(self, vpath: str) -> str:
        """将 FUSE 虚拟路径转换为 COS 前缀（以 / 结尾）"""
        if vpath == "/" or vpath == "":
            return self.cos_prefix
        clean = vpath.strip("/")
        # sandbox 模式：/workspace/xxx → agents/{agent_hash}/workspace/xxx
        if self.agent_hash:
            if clean == "workspace" or clean.startswith("workspace/"):
                clean = f"agents/{self.agent_hash}/{clean}"
        return f"{self.cos_prefix}{clean}/"

    def _vpath_to_cos_key(self, vpath: str) -> str:
        """将 FUSE 虚拟路径转换为 COS key（文件用，不以 / 结尾）"""
        if vpath == "/":
            return self.cos_prefix.rstrip("/")
        clean = vpath.strip("/")
        # sandbox 模式：/workspace/xxx → agents/{agent_hash}/workspace/xxx
        if self.agent_hash:
            if clean == "workspace" or clean.startswith("workspace/"):
                clean = f"agents/{self.agent_hash}/{clean}"
        return f"{self.cos_prefix}{clean}"

    def _cos_list_objects(self, prefix: str) -> list:
        """用 StorageService 直接列出 COS 对象（不需要锁）"""
        result = self.vfs.storage.list_objects(prefix=prefix)
        return result if result else []

    def _cos_get_content(self, key: str) -> bytes:
        """用 StorageService 直接读取 COS 对象（不需要锁，COS 原子操作）"""
        return self.vfs.storage.get_file_content(key)

    def _cos_parse_dir(self, cos_prefix: str) -> list:
        """解析 COS prefix 下的直接子项，返回 VirtualDirEntry 列表"""
        from services.virtual_filesystem import VirtualDirEntry

        objects = self._cos_list_objects(cos_prefix)
        entries: Dict[str, VirtualDirEntry] = {}
        base_len = len(cos_prefix)

        for obj in objects:
            key = obj["Key"]
            rel_path = key[base_len:].lstrip("/")

            if "/" in rel_path:
                # 子目录
                dir_name = rel_path.split("/")[0]
                if dir_name not in entries:
                    entries[dir_name] = VirtualDirEntry(
                        name=dir_name,
                        path=dir_name,
                        type="directory",
                        size=4096,
                        mtime=0,
                        mode="drwxr-xr-x",
                        inode=self.vfs._gen_inode(cos_prefix + dir_name),
                        is_hidden=dir_name.startswith("."),
                        nlink=2,
                    )
            else:
                # 文件
                name = rel_path
                if name not in entries:
                    size = int(obj.get("Size", 0) or 0)
                    mtime = 0.0
                    last_mod = obj.get("LastModified", "")
                    if last_mod:
                        from services.virtual_filesystem import FileEntry
                        mtime = FileEntry._parse_cos_date(last_mod)
                    entries[name] = VirtualDirEntry(
                        name=name,
                        path=rel_path,
                        type="file",
                        size=size,
                        mtime=mtime,
                        mode="-rw-r--r--",
                        inode=self.vfs._gen_inode(key),
                        is_hidden=name.startswith("."),
                        nlink=1,
                    )

        result = list(entries.values())
        result.sort(key=lambda e: (e.type != "directory", e.name))
        return result

    def _vpath_to_config_key(self, vpath: str) -> str:
        """将 FUSE 相对路径转为 DB config key

        sandbox 模式下根级文件（persona, tools 等）→ agents/{hash}/{key}
        其他路径（global/xxx, channels/feishu/xxx 等）直接返回
        """
        if self.agent_hash and "/" not in vpath:
            return f"agents/{self.agent_hash}/{vpath}"
        return vpath

    def _config_key_to_vpath(self, db_key: str) -> str:
        """将 DB config key 转为 FUSE 相对路径

        agents/{hash}/xxx → xxx（sandbox 模式下隐去 agent_hash 前缀）
        其他直接返回
        """
        if db_key.startswith("agents/"):
            parts = db_key.split("/", 2)
            if len(parts) >= 3:
                return parts[2]
        return db_key

    def _get_config_dir_entries(self, rel_path: str) -> list:
        """从 _CONFIG_DIR_TREE 获取指定路径下的虚拟条目

        Args:
            rel_path: 相对于 /config/ 的路径，"" 表示 /config/ 根目录

        Returns:
            VirtualDirEntry 列表
        """
        from services.virtual_filesystem import VirtualDirEntry
        from models.database import AgentConfig, SessionLocal

        node = self._CONFIG_DIR_TREE.get(rel_path)
        if not node:
            return []

        entries = []

        # 子目录
        for dir_name in node.get("dirs", []):
            full_rel = f"{rel_path}/{dir_name}" if rel_path else dir_name
            entries.append(VirtualDirEntry(
                name=dir_name,
                path=f"/config/{full_rel}",
                type="directory",
                size=4096,
                mtime=0,
                mode="drwxr-xr-x",
                inode=self._get_inode(f"/config/{full_rel}"),
                is_hidden=False,
                nlink=2,
            ))

        # 文件
        for file_name in node.get("files", []):
            full_rel = f"{rel_path}/{file_name}" if rel_path else file_name
            db_key = self._vpath_to_config_key(full_rel)

            # 从 DB 查询实际内容大小和修改时间
            size = 0
            mtime = 0.0
            is_writable = bool(self.agent_hash)  # sandbox 模式可写
            if rel_path.startswith("platform"):
                is_writable = False  # platform 始终只读

            db = SessionLocal()
            try:
                q = db.query(AgentConfig).filter(AgentConfig.key == db_key)
                if self.agent_hash and db_key.startswith("agents/"):
                    q = q.filter(AgentConfig.agent_hash == self.agent_hash)
                q = q.filter(AgentConfig.permission != "none")
                cfg = q.first()
                if cfg and cfg.value:
                    size = len((cfg.value or "").encode("utf-8") if isinstance(cfg.value, str) else (cfg.value or b""))
            finally:
                db.close()

            entries.append(VirtualDirEntry(
                name=file_name,
                path=f"/config/{full_rel}",
                type="file",
                size=size,
                mtime=mtime,
                mode="-rw-r--r--" if is_writable else "-r--r--r--",
                inode=self._get_inode(f"/config/{full_rel}"),
                is_hidden=False,
                nlink=1,
            ))

        entries.sort(key=lambda e: (e.type != "directory", e.name))
        return entries

    def _get_root_entries(self) -> list:
        """返回根目录的硬编码虚拟条目列表"""
        from services.virtual_filesystem import VirtualDirEntry
        entries = []
        for name in self.ROOT_DIRS:
            entries.append(VirtualDirEntry(
                name=name,
                path=name,
                type="directory",
                size=4096,
                mtime=0,
                mode="drwxr-xr-x",
                inode=self._get_inode(f"/{name}"),
                is_hidden=False,
                nlink=2,
            ))
        return entries

    # ── 内部辅助方法 ──────────────────────────────────────────────

    def _resolve_path(self, inode: int) -> str:
        """通过 inode 反查虚拟路径"""
        path = self._inode_to_path.get(inode)
        if path is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return path

    def _get_inode(self, vpath: str) -> int:
        """获取虚拟路径的 inode，不存在则分配新 inode"""
        vpath = vpath.rstrip("/") or "/"
        with self._inode_lock:
            if vpath in self._path_to_inode:
                return self._path_to_inode[vpath]
            inode = self._next_inode
            self._next_inode += 1
            self._path_to_inode[vpath] = inode
            self._inode_to_path[inode] = vpath
            return inode

    def _ensure_inode(self, vpath: str) -> int:
        """确保路径有 inode（仅注册已存在的）"""
        vpath = vpath.rstrip("/") or "/"
        with self._inode_lock:
            if vpath in self._path_to_inode:
                return self._path_to_inode[vpath]
            # /config/ 路径没有 COS key，用简单递增 inode
            if vpath.startswith("/config"):
                return self._get_inode(vpath)
            # 使用我们自己的 COS key 映射 + VFS 的 inode 生成
            cos_key = self._vpath_to_cos_key(vpath)
            inode = self.vfs._gen_inode(cos_key)
            # 确保 inode 不冲突且 > 0（带最大重试限制，防止无限循环）
            max_retries = 10000
            retries = 0
            while (inode in self._inode_to_path or inode == 0) and retries < max_retries:
                inode = (inode + 1) % (10 ** 8)
                if inode == 0:
                    inode = 1
                retries += 1
            if retries >= max_retries:
                raise RuntimeError("Inode exhaustion")
            self._path_to_inode[vpath] = inode
            self._inode_to_path[inode] = vpath
            return inode

    def _vpath_from_parent(self, parent_inode: int, name: str) -> str:
        """从父 inode 和子名拼接虚拟路径"""
        parent_path = self._resolve_path(parent_inode)
        if parent_path == "/":
            return f"/{name}"
        return f"{parent_path}/{name}"

    def _get_entry_attr(self, vpath: str, entry: "VirtualDirEntry") -> pyfuse3.EntryAttributes:
        """从 VirtualDirEntry 构造 EntryAttributes"""
        inode = self._ensure_inode(vpath)
        return _make_attr(
            inode=inode,
            entry_type=entry.type,
            size=entry.size,
            mtime=entry.mtime,
            nlink=entry.nlink,
            uid=self.uid,
            gid=self.gid,
            cache_ttl=self.cache_ttl,
        )

    def _invalidate_dir_cache(self, vpath: str):
        """使父目录的 VFS 目录缓存失效"""
        vpath = vpath.rstrip("/")
        parent = vpath.rsplit("/", 1)[0] if "/" in vpath else "/"
        if not parent:
            parent = "/"
        if parent == "/":
            self.vfs._dir_cache.pop(self.cos_prefix, None)
        else:
            cos_prefix = self._cos_prefix_for(parent)
            self.vfs._dir_cache.pop(cos_prefix, None)

    def _check_not_public(self, cos_key: str):
        """检查 COS key 是否在 public 路径下（public 是只读的）"""
        public_prefix = f"{self.cos_prefix}public/"
        if cos_key.startswith(public_prefix):
            raise pyfuse3.FUSEError(errno.EACCES)

    def _check_agents_path(self, config_key: str):
        """验证 config_key 中的 agent_hash 是否匹配当前实例

        防止 sandbox 模式下通过 /config/agents/{other_hash}/ 路径遍历到其他 Agent。
        """
        if config_key.startswith("agents/") and self.agent_hash:
            parts = config_key.split("/")
            if len(parts) >= 2:
                key_hash = parts[1]
                if key_hash != self.agent_hash:
                    raise pyfuse3.FUSEError(errno.EACCES)

    def _find_entry(self, parent_inode: int, name: str) -> Optional[Tuple[str, "VirtualDirEntry"]]:
        """在父目录中查找名为 name 的子条目，返回 (vpath, entry) 或 None"""
        parent_path = self._resolve_path(parent_inode)
        child_path = self._vpath_from_parent(parent_inode, name)

        # 根目录：检查硬编码虚拟条目
        if parent_path == "/":
            for entry in self._get_root_entries():
                if entry.name == name:
                    return (child_path, entry)
            return None

        # /config/ 下的文件或目录：从树结构查询
        if parent_path == "/config":
            entries = self._get_config_dir_entries("")
        elif parent_path.startswith("/config/"):
            rel_path = parent_path[len("/config/"):]
            entries = self._get_config_dir_entries(rel_path)
        else:
            entries = None

        if entries is not None:
            for entry in entries:
                if entry.name == name:
                    return (child_path, entry)
            return None

        # 子路径：直接查询 COS
        cos_prefix = self._cos_prefix_for(parent_path)
        try:
            entries = self._cos_parse_dir(cos_prefix)
        except Exception as e:
            logger.warning(f"FUSE _find_entry error: {e}")
            return None

        for entry in entries:
            if entry.name == name:
                return (child_path, entry)
        return None

    def _get_dir_entries(self, parent_inode: int):
        """获取目录内容列表（包含 . 和 ..）"""
        from services.virtual_filesystem import VirtualDirEntry

        parent_path = self._resolve_path(parent_inode)

        # /config/ 目录：从树结构获取虚拟条目
        if parent_path == "/config":
            entries = self._get_config_dir_entries("")
        elif parent_path.startswith("/config/"):
            rel_path = parent_path[len("/config/"):]
            entries = self._get_config_dir_entries(rel_path)
        # 根目录：返回硬编码虚拟条目
        elif parent_path == "/":
            entries = self._get_root_entries()
        else:
            # 子路径：直接查询 COS
            cos_prefix = self._cos_prefix_for(parent_path)
            try:
                entries = self._cos_parse_dir(cos_prefix)
            except Exception as e:
                logger.warning(f"FUSE _get_dir_entries error: {e}")
                entries = []

        # 过滤 VFS 内部标记文件 .directory
        entries = [e for e in entries if e.name != ".directory"]

        # 添加 . 和 ..
        dot = VirtualDirEntry(
            name=".", path="", type="directory", size=4096,
            mtime=0, mode="drwxr-xr-x", inode=parent_inode, is_hidden=False, nlink=2
        )
        if parent_path == "/":
            parent_of_parent = "/"
        else:
            parent_path_clean = parent_path.rstrip("/")
            parent_of_parent = parent_path_clean.rsplit("/", 1)[0] if "/" in parent_path_clean else "/"
        parent_inode_id = self._get_inode(parent_of_parent)
        dotdot = VirtualDirEntry(
            name="..", path="", type="directory", size=4096,
            mtime=0, mode="drwxr-xr-x", inode=parent_inode_id, is_hidden=False, nlink=2
        )

        return [dot, dotdot] + entries

    # ── FUSE 回调方法 ─────────────────────────────────────────────

    async def lookup(self, parent_inode: int, name: bytes,
                     ctx: pyfuse3.RequestContext) -> pyfuse3.EntryAttributes:
        """在父目录中查找子条目"""
        name_str = name.decode("utf-8", errors="replace")
        parent_path = self._resolve_path(parent_inode)
        logger.info(f"FUSE lookup: parent_inode={parent_inode}({parent_path}), name={name_str}")
        result = self._find_entry(parent_inode, name_str)
        if result is None:
            logger.warning(f"FUSE lookup: NOT FOUND parent_inode={parent_inode}({parent_path}), name={name_str}")
            raise pyfuse3.FUSEError(errno.ENOENT)
        vpath, entry = result
        logger.info(f"FUSE lookup: FOUND vpath={vpath}, type={entry.type}, size={entry.size}")
        return self._get_entry_attr(vpath, entry)

    async def getattr(self, inode: int,
                      ctx: pyfuse3.RequestContext) -> pyfuse3.EntryAttributes:
        """获取 inode 的属性"""
        vpath = self._resolve_path(inode)

        # 根目录特殊处理
        if vpath == "/":
            return _make_attr(
                inode=ROOT_INODE, entry_type="directory", size=4096,
                mtime=0, nlink=2, uid=self.uid, gid=self.gid,
                cache_ttl=self.cache_ttl,
            )

        # 通过父目录查找
        parent_path = "/" + "/".join(vpath.strip("/").split("/")[:-1])
        if not parent_path or parent_path == "":
            parent_path = "/"
        name = vpath.rstrip("/").split("/")[-1]

        parent_inode_num = self._get_inode(parent_path)
        result = self._find_entry(parent_inode_num, name)
        if result is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        _, entry = result
        return self._get_entry_attr(vpath, entry)

    async def opendir(self, inode: int,
                      ctx: pyfuse3.RequestContext) -> int:
        """打开目录，准备读取"""
        entries = self._get_dir_entries(inode)

        # 构建目录条目列表，包含 name_bytes, attr, entry_id
        dir_data = []
        for i, entry in enumerate(entries):
            name_bytes = entry.name.encode("utf-8", errors="replace")

            if entry.name == ".":
                path_for_attr = "/"
                entry_inode = inode
            elif entry.name == "..":
                parent_path = self._resolve_path(inode).rstrip("/")
                parent = parent_path.rsplit("/", 1)[0] if "/" in parent_path else "/"
                path_for_attr = parent
                entry_inode = self._get_inode(parent)
            else:
                path_for_attr = self._vpath_from_parent(inode, entry.name)
                entry_inode = self._ensure_inode(path_for_attr)

            attr = _make_attr(
                inode=entry_inode,
                entry_type=entry.type,
                size=entry.size,
                mtime=entry.mtime,
                nlink=entry.nlink,
                uid=self.uid,
                gid=self.gid,
                cache_ttl=self.cache_ttl,
            )
            dir_data.append((name_bytes, attr, i + 1))

        fh = self._next_fh
        self._next_fh += 1
        self._dir_handles[fh] = dir_data
        return fh

    async def readdir(self, fh: int, start_id: int,
                      token: pyfuse3.ReaddirToken) -> None:
        """读取目录条目"""
        entries = self._dir_handles.get(fh, [])
        for name_bytes, attr, entry_id in entries:
            if entry_id <= start_id:
                continue
            pyfuse3.readdir_reply(token, name_bytes, attr, entry_id)

    async def releasedir(self, fh: int) -> None:
        """释放目录句柄"""
        self._dir_handles.pop(fh, None)

    async def open(self, inode: int, flags: int,
                   ctx: pyfuse3.RequestContext) -> pyfuse3.FileInfo:
        """打开文件"""
        vpath = self._resolve_path(inode)
        if vpath == "/":
            raise pyfuse3.FUSEError(errno.EISDIR)

        # 验证文件存在
        parent_path = "/" + "/".join(vpath.strip("/").split("/")[:-1])
        if not parent_path or parent_path == "":
            parent_path = "/"
        name = vpath.rstrip("/").split("/")[-1]
        parent_inode = self._get_inode(parent_path)
        if self._find_entry(parent_inode, name) is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        fh = self._next_fh
        self._next_fh += 1
        self._open_files[fh] = {"inode": inode, "path": vpath}
        return pyfuse3.FileInfo(fh=fh)

    async def read(self, fh: int, offset: int, size: int) -> bytes:
        """读取文件内容（直接 COS 访问，COS 自身保证原子性）"""
        file_info = self._open_files.get(fh)
        if file_info is None:
            raise pyfuse3.FUSEError(errno.EBADF)

        vpath = file_info["path"]

        # /config/ 路径：从数据库读取（虚拟配置，按 agent_hash+key 过滤，权限检查）
        # 注意：同步 DB 查询会阻塞 Trio 事件循环。这是架构性限制，短期内需接受。
        if vpath.startswith("/config/"):
            rel_path = vpath[len("/config/"):].lstrip("/")
            config_key = self._vpath_to_config_key(rel_path)
            from models.database import AgentConfig, SessionLocal
            db = SessionLocal()
            try:
                q = db.query(AgentConfig).filter(AgentConfig.key == config_key)
                if self.agent_hash and config_key.startswith("agents/"):
                    q = q.filter(AgentConfig.agent_hash == self.agent_hash)
                q = q.filter(AgentConfig.permission != "none")
                cfg = q.first()
                content = (cfg.value or "").encode("utf-8") if cfg else b""
            finally:
                db.close()
            if offset >= len(content):
                return b""
            return content[offset:offset + size]

        # 普通 COS 文件
        cos_key = self._vpath_to_cos_key(vpath)
        content_bytes = self._cos_get_content(cos_key)
        if content_bytes is None:
            raise pyfuse3.FUSEError(errno.EIO)

        data = content_bytes
        if offset >= len(data):
            return b""
        return data[offset:offset + size]

    async def write(self, fh: int, offset: int, buf: bytes) -> int:
        """写入文件内容（直接 COS 访问，per-file 锁防止 RMW 竞态）"""
        file_info = self._open_files.get(fh)
        if file_info is None:
            logger.warning(f"FUSE write: invalid fh={fh}")
            raise pyfuse3.FUSEError(errno.EBADF)

        vpath = file_info["path"]

        # /config/ 路径：写入数据库（虚拟配置，按 agent_hash+key 过滤，权限检查）
        if vpath.startswith("/config/"):
            rel_path = vpath[len("/config/"):].lstrip("/")
            # platform 始终只读
            if rel_path.startswith("platform/"):
                raise pyfuse3.FUSEError(errno.EACCES)
            config_key = self._vpath_to_config_key(rel_path)
            self._check_agents_path(config_key)
            # 全局 FUSE 下的 /config/ 只读（除了 agent 级路径在 sandbox 模式下可写）
            if not self.agent_hash:
                raise pyfuse3.FUSEError(errno.EACCES)
            value = buf.decode("utf-8", errors="replace")
            from models.database import AgentConfig, SessionLocal
            db = SessionLocal()
            try:
                q = db.query(AgentConfig).filter(AgentConfig.key == config_key)
                if config_key.startswith("agents/"):
                    q = q.filter(AgentConfig.agent_hash == self.agent_hash)
                cfg = q.first()
                # 权限检查：read 和 none 不允许写入
                if cfg and cfg.permission in ("read", "none"):
                    logger.warning(f"FUSE config write denied: {config_key} permission={cfg.permission}")
                    raise pyfuse3.FUSEError(errno.EACCES)
                if cfg:
                    cfg.value = value
                else:
                    db.add(AgentConfig(key=config_key, value=value,
                                       agent_hash=self.agent_hash if config_key.startswith("agents/") else None))
                db.commit()
            except pyfuse3.FUSEError:
                raise
            except Exception as e:
                logger.error(f"FUSE config write error: {e}")
                raise pyfuse3.FUSEError(errno.EIO)
            finally:
                db.close()
            return len(buf)

        cos_key = self._vpath_to_cos_key(vpath)
        self._check_not_public(cos_key)
        logger.info(f"FUSE write: fh={fh}, path={vpath}, offset={offset}, size={len(buf)}")

        # per-file 锁，只对同一个 cos_key 互斥
        if cos_key not in self._file_locks:
            self._file_locks[cos_key] = trio.Lock()
        async with self._file_locks[cos_key]:
            if offset > 0:
                # 非 offset=0 写入：需要先读后写
                existing_bytes = self._cos_get_content(cos_key)
                if existing_bytes is None:
                    existing_bytes = b""
                if len(existing_bytes) < offset:
                    existing_bytes += b"\0" * (offset - len(existing_bytes))
                full = existing_bytes[:offset] + buf
            else:
                full = buf

            # 直接写入 COS（buf 保持 bytes，不做编解码）
            self.vfs.storage.put_object(cos_key, full)
            logger.info(f"FUSE write: OK {len(buf)} bytes to {cos_key}")

        # 写操作后清除父目录缓存
        self._invalidate_dir_cache(vpath)
        return len(buf)

    async def create(self, parent_inode: int, name: bytes, mode: int, flags: int,
                     ctx: pyfuse3.RequestContext) -> Tuple[pyfuse3.FileInfo, pyfuse3.EntryAttributes]:
        """创建新文件（COS 或 Config 数据库）"""
        name_str = name.decode("utf-8", errors="replace")
        vpath = self._vpath_from_parent(parent_inode, name_str)

        # /config/ 路径：在数据库中创建配置项（按 agent_hash+key 过滤，权限检查）
        if vpath.startswith("/config/"):
            rel_path = vpath[len("/config/"):].lstrip("/")
            # platform 始终只读
            if rel_path.startswith("platform/"):
                raise pyfuse3.FUSEError(errno.EACCES)
            config_key = self._vpath_to_config_key(rel_path)
            self._check_agents_path(config_key)
            # 全局 FUSE 下的 /config/ 只读
            if not self.agent_hash:
                raise pyfuse3.FUSEError(errno.EACCES)
            from models.database import AgentConfig, SessionLocal
            db = SessionLocal()
            try:
                q = db.query(AgentConfig).filter(AgentConfig.key == config_key)
                if config_key.startswith("agents/"):
                    q = q.filter(AgentConfig.agent_hash == self.agent_hash)
                exists = q.first()
                if exists and exists.permission in ("read", "none"):
                    logger.warning(f"FUSE config create denied: {config_key} permission={exists.permission}")
                    raise pyfuse3.FUSEError(errno.EACCES)
                if not exists:
                    db.add(AgentConfig(key=config_key, value="",
                                       agent_hash=self.agent_hash if config_key.startswith("agents/") else None))
                    db.commit()
            except pyfuse3.FUSEError:
                raise
            except Exception as e:
                logger.error(f"FUSE config create error: {e}")
                raise pyfuse3.FUSEError(errno.EIO)
            finally:
                db.close()
            inode = self._ensure_inode(vpath)
            fh = self._next_fh
            self._next_fh += 1
            self._open_files[fh] = {"inode": inode, "path": vpath}
            attr = _make_attr(
                inode=inode, entry_type="file", size=0, mtime=0,
                nlink=1, uid=self.uid, gid=self.gid, cache_ttl=self.cache_ttl,
            )
            return (pyfuse3.FileInfo(fh=fh), attr)

        cos_key = self._vpath_to_cos_key(vpath)
        self._check_not_public(cos_key)
        self.vfs.storage.put_object(cos_key, b"")
        logger.info(f"FUSE create: {cos_key}")

        # 清除父目录缓存
        self._invalidate_dir_cache(vpath)

        inode = self._ensure_inode(vpath)
        fh = self._next_fh
        self._next_fh += 1
        self._open_files[fh] = {"inode": inode, "path": vpath}

        attr = _make_attr(
            inode=inode, entry_type="file", size=0, mtime=0,
            nlink=1, uid=self.uid, gid=self.gid, cache_ttl=self.cache_ttl,
        )
        return (pyfuse3.FileInfo(fh=fh), attr)

    async def mknod(self, parent_inode: int, name: bytes, mode: int, rdev: int,
                    ctx: pyfuse3.RequestContext) -> pyfuse3.EntryAttributes:
        """创建文件节点（某些内核路径的创建入口，直接 COS 操作）"""
        name_str = name.decode("utf-8", errors="replace")
        vpath = self._vpath_from_parent(parent_inode, name_str)
        if vpath.startswith("/config/"):
            raise pyfuse3.FUSEError(errno.EACCES)
        cos_key = self._vpath_to_cos_key(vpath)
        self._check_not_public(cos_key)
        self.vfs.storage.put_object(cos_key, b"")
        logger.info(f"FUSE mknod: {cos_key}")

        inode = self._ensure_inode(vpath)
        self._invalidate_dir_cache(vpath)
        return _make_attr(
            inode=inode, entry_type="file", size=0, mtime=0,
            nlink=1, uid=self.uid, gid=self.gid, cache_ttl=self.cache_ttl,
        )

    async def mkdir(self, parent_inode: int, name: bytes, mode: int,
                    ctx: pyfuse3.RequestContext) -> pyfuse3.EntryAttributes:
        """创建目录（直接 COS 操作，用 .placeholder 标记空目录）"""
        name_str = name.decode("utf-8", errors="replace")
        vpath = self._vpath_from_parent(parent_inode, name_str)
        cos_key = self._vpath_to_cos_key(vpath)
        self._check_not_public(cos_key)
        self.vfs.storage.put_object(cos_key.rstrip("/") + "/.placeholder", b"")
        logger.info(f"FUSE mkdir: {cos_key}")

        inode = self._ensure_inode(vpath)
        self._invalidate_dir_cache(vpath)
        return _make_attr(
            inode=inode, entry_type="directory", size=4096, mtime=0,
            nlink=2, uid=self.uid, gid=self.gid, cache_ttl=self.cache_ttl,
        )

    async def unlink(self, parent_inode: int, name: bytes,
                     ctx: pyfuse3.RequestContext) -> None:
        """删除文件（直接 COS 操作）"""
        name_str = name.decode("utf-8", errors="replace")
        vpath = self._vpath_from_parent(parent_inode, name_str)

        # 检查存在
        result = self._find_entry(parent_inode, name_str)
        if result is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        child_path, entry = result
        if entry.type == "directory":
            raise pyfuse3.FUSEError(errno.EISDIR)

        cos_key = self._vpath_to_cos_key(vpath)
        self._check_not_public(cos_key)
        self.vfs.storage.delete_file_by_key(cos_key)
        logger.info(f"FUSE unlink: {cos_key}")
        self._invalidate_dir_cache(vpath)

    async def rmdir(self, parent_inode: int, name: bytes,
                    ctx: pyfuse3.RequestContext) -> None:
        """删除目录（直接 COS 操作，检查非空）"""
        name_str = name.decode("utf-8", errors="replace")
        vpath = self._vpath_from_parent(parent_inode, name_str)
        cos_key = self._vpath_to_cos_key(vpath)
        self._check_not_public(cos_key)
        cos_prefix = self._cos_prefix_for(vpath)
        objects = self._cos_list_objects(cos_prefix)
        # 过滤 COS 目录标记文件（.placeholder / .directory），剩下的才是实际内容
        actual = [o for o in objects
                  if o['Key'] != cos_prefix + ".placeholder"
                  and o['Key'] != cos_prefix + ".directory"
                  and not o['Key'].endswith('/')]
        if actual:
            raise pyfuse3.FUSEError(errno.ENOTEMPTY)
        # 删除该 prefix 下所有对象（包括目录标记文件）
        for obj in objects:
            self.vfs.storage.delete_file_by_key(obj['Key'])
        logger.info(f"FUSE rmdir: {cos_prefix} ({len(objects)} objects)")
        self._invalidate_dir_cache(vpath)

    async def rename(self, old_parent_inode: int, old_name: bytes,
                     new_parent_inode: int, new_name: bytes, flags: int,
                     ctx: pyfuse3.RequestContext) -> None:
        """重命名/移动文件或目录（直接 COS copy + delete）"""
        old_name_str = old_name.decode("utf-8", errors="replace")
        new_name_str = new_name.decode("utf-8", errors="replace")
        src = self._vpath_from_parent(old_parent_inode, old_name_str)
        dst = self._vpath_from_parent(new_parent_inode, new_name_str)
        src_key = self._vpath_to_cos_key(src)
        dst_key = self._vpath_to_cos_key(dst)
        # public 是只读的，禁止 rename 进出
        self._check_not_public(src_key)
        self._check_not_public(dst_key)

        # 判断是文件还是目录：检查 COS prefix 下是否有子对象
        src_prefix = self._cos_prefix_for(src)
        objects = self._cos_list_objects(src_prefix)
        is_dir = len(objects) > 0

        if is_dir:
            # 目录：递归复制所有子对象
            for obj in objects:
                src_obj_key = obj['Key']
                rel = src_obj_key[len(src_prefix):]
                dst_obj_key = dst_key.rstrip("/") + "/" + rel
                # 获取内容并写入新位置
                content = self._cos_get_content(src_obj_key)
                if content is not None:
                    self.vfs.storage.put_object(dst_obj_key, content)
                self.vfs.storage.delete_file_by_key(src_obj_key)
        else:
            # 文件：获取内容 → 写入新位置 → 删除旧位置
            content = self._cos_get_content(src_key)
            if content is not None:
                self.vfs.storage.put_object(dst_key, content)
            self.vfs.storage.delete_file_by_key(src_key)

        logger.info(f"FUSE rename: {src_key} -> {dst_key} (is_dir={is_dir})")

        # 更新 inode 映射（受锁保护）
        with self._inode_lock:
            if src in self._path_to_inode:
                inode = self._path_to_inode.pop(src)
                self._path_to_inode[dst] = inode
                self._inode_to_path[inode] = dst
        self._invalidate_dir_cache(src)
        self._invalidate_dir_cache(dst)

    async def forget(self, inode_list) -> None:
        """忘记 inode（内核缓存清理）"""
        with self._inode_lock:
            for inode, n in inode_list:
                self._inode_to_path.pop(inode, None)
            # 清理 path 映射中已删除的 inode
            for path, i in list(self._path_to_inode.items()):
                if i not in self._inode_to_path:
                    self._path_to_inode.pop(path, None)

    async def setattr(self, inode: int, attr: pyfuse3.EntryAttributes,
                      fields: pyfuse3.SetattrFields, fh: Optional[int],
                      ctx: pyfuse3.RequestContext) -> pyfuse3.EntryAttributes:
        """设置文件属性（chmod/chown/truncate 等）"""
        # 目前仅支持读取当前属性
        return await self.getattr(inode, ctx)

    async def flush(self, fh: int) -> None:
        """刷新文件缓冲区"""
        # 无操作，VFS 即时写入
        pass

    async def release(self, fh: int) -> None:
        """释放文件句柄"""
        self._open_files.pop(fh, None)

    async def statfs(self, ctx: pyfuse3.RequestContext) -> pyfuse3.StatvfsData:
        """文件系统统计信息（静态占位符，非真实 COS 容量）"""
        stats = pyfuse3.StatvfsData()
        stats.f_bsize = 512
        stats.f_frsize = 512
        stats.f_blocks = 100 * 1024 * 1024 // 512  # 100 GB (估算)
        stats.f_bfree = 50 * 1024 * 1024 // 512
        stats.f_bavail = 50 * 1024 * 1024 // 512
        stats.f_files = 1000000
        stats.f_ffree = 500000
        stats.f_favail = 500000
        stats.f_flag = 0
        stats.f_namemax = 255
        return stats

    @classmethod
    def create_for_sandbox(cls, vfs, mount_dir: str, agent_hash: str):
        """创建 sandbox 专用的 FUSE 实例"""
        return cls(vfs, mount_dir, cache_ttl=60, cos_prefix="feclaw/",
                   agent_hash=agent_hash)


# ── 后台启动 / 卸载 ──────────────────────────────────────────────

import trio


def start_fuse_background(vfs, mount_dir, cache_ttl=60, cos_prefix="feclaw/",
                          agent_hash=None):
    """在后台线程启动 FUSE 守护进程"""
    import threading
    import subprocess

    def _run():
        try:
            # 清理旧的挂载
            subprocess.run(["fusermount3", "-u", mount_dir], capture_output=True)
            subprocess.run(["fusermount3", "-uz", mount_dir], capture_output=True)
            os.makedirs(mount_dir, exist_ok=True)

            async def _fuse_main():
                ops = VFSFuseDaemon(vfs, mount_dir, cache_ttl,
                                    cos_prefix=cos_prefix, agent_hash=agent_hash)
                pyfuse3.init(ops, mount_dir, set())
                logger.info(f"FUSE daemon mounted at {mount_dir}")
                await pyfuse3.main()

            # pyfuse3 基于 Trio，必须用 trio.run()
            trio.run(_fuse_main)
        except Exception as e:
            logger.error(f"FUSE daemon error: {e}")

    os.makedirs(mount_dir, exist_ok=True)
    thread = threading.Thread(target=_run, daemon=True, name="fuse-daemon")
    thread.start()
    return thread


def unmount_fuse(mount_dir):
    """卸载 FUSE 挂载点"""
    import subprocess
    try:
        subprocess.run(["fusermount3", "-u", mount_dir], capture_output=True, timeout=10)
        logger.info(f"FUSE unmounted: {mount_dir}")
    except Exception as e:
        logger.warning(f"FUSE unmount warning: {e}")


# === Sandbox FUSE 生命周期管理 ===

import time as _time_module

# 模块级注册表: {mount_dir: {agent_hash, last_active, created_at}}
_SANDBOX_FUSE_REGISTRY = {}


def register_sandbox_fuse(mount_dir: str, agent_hash: str = None):
    """注册 sandbox 专属 FUSE 实例"""
    _SANDBOX_FUSE_REGISTRY[mount_dir] = {
        "agent_hash": agent_hash or "",
        "last_active": _time_module.time(),
        "created_at": _time_module.time(),
    }


def touch_sandbox_fuse(mount_dir: str):
    """标记 FUSE 活跃"""
    entry = _SANDBOX_FUSE_REGISTRY.get(mount_dir)
    if entry:
        entry["last_active"] = _time_module.time()


def cleanup_stale_fuses(timeout: int = 1800) -> int:
    """清理超过 timeout 秒未活跃的 FUSE 实例

    Args:
        timeout: 超时秒数（默认 30 分钟）

    Returns:
        清理的 FUSE 实例数
    """
    now = _time_module.time()
    stale = [
        (d, e) for d, e in _SANDBOX_FUSE_REGISTRY.items()
        if now - e["last_active"] > timeout
    ]

    count = 0
    for mount_dir, entry in stale:
        try:
            unmount_fuse(mount_dir)
            import os as _os
            _os.rmdir(mount_dir)
        except Exception as e:
            logger.warning(f"Failed to cleanup FUSE {mount_dir}: {e}")
        finally:
            _SANDBOX_FUSE_REGISTRY.pop(mount_dir, None)
            count += 1
            logger.info(f"Cleaned stale sandbox FUSE: {mount_dir} (agent={entry['agent_hash']})")

    return count


def get_active_fuse_count() -> int:
    """返回当前活跃的 sandbox FUSE 实例数"""
    return len(_SANDBOX_FUSE_REGISTRY)


# ── FUSE 健康检查 watchdog（Level 2 自动恢复） ────────────────

def fuse_is_alive(mount_dir: str) -> bool:
    """Real health check — try to list the mount directory.

    Returns False if:
    - mount_dir doesn't exist
    - mount_dir returns 'Transport endpoint is not connected' (zombie)
    - any other OS error
    """
    if not os.path.exists(mount_dir):
        return False
    try:
        os.listdir(mount_dir)
        return True
    except OSError:
        return False


def fuse_health_watchdog(mount_dir: str, vfs, cache_ttl: int = 60,
                         cos_prefix: str = "feclaw/"):
    """Background thread that checks FUSE health every 30 seconds.

    If FUSE dies, clean up the zombie mount and restart the daemon.
    This runs in a daemon thread so it won't block shutdown.
    """
    import threading
    import subprocess
    import time as _time

    check_interval = 30

    while True:
        _time.sleep(check_interval)

        if fuse_is_alive(mount_dir):
            continue

        logger.warning(
            f"FUSE health check failed for {mount_dir} — "
            f"attempting auto-recovery"
        )

        try:
            # 强制卸载 zombie 挂载
            subprocess.run(
                ["fusermount3", "-u", mount_dir],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["fusermount3", "-uz", mount_dir],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            logger.error(f"FUSE watchdog cleanup error: {e}")

        try:
            # 重启 FUSE 守护进程（在独立 daemon 线程中运行）
            restart_thread = threading.Thread(
                target=start_fuse_background,
                args=(vfs, mount_dir, cache_ttl, cos_prefix),
                daemon=True,
                name="fuse-daemon-restarted",
            )
            restart_thread.start()
            logger.info(f"FUSE auto-recovery initiated for {mount_dir}")
        except Exception as e:
            logger.error(f"FUSE watchdog restart failed: {e}")
