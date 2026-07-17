"""
Path utilities for VirtualFileSystem - COS 路径映射 + path 合法性检查
"""
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# P1.2: Golden Rule -- 禁止 COS key 拼接时 workspace 段重复
# ────────────────────────────────────────────────────────────────────
# Golden Rule 的含义：VFS path 自身可以包含 /workspace/...（合法访问），
# 但 COS key 拼接时，如果 base_path 已以 workspace/ 结尾，
# 而 VFS path 又以 /workspace/ 开头，就会出现 .../workspace/workspace/... 的重复。
# 本函数检测这种情况，宽容模式下仅 warning，严格模式下 raise。

# 全局开关：False = 宽容模式（warning），True = 严格模式（raise）
# 建议在 1-2 个迭代后切换为 True
GOLDEN_RULE_STRICT = False


def check_golden_rule(base_path: str, vfs_path: str) -> Optional[str]:
    """检查 COS key 拼接是否存在 workspace 段重复。

    Args:
        base_path: COS 基础前缀，如 "feclaw/agents/abc1/" 或 "feclaw/agents/abc1/workspace/"
        vfs_path: VFS 路径（已去除前导 /），如 "workspace/foo" 或 "agent/config.md"

    Returns:
        错误消息字符串（如果检测到违规），None 表示通过。
    """
    # 检测 base_path 是否以 workspace/ 结尾
    base_ends_with_workspace = base_path.rstrip("/").endswith("/workspace") or base_path.endswith("workspace/")
    # 检测 vfs_path 是否以 workspace/ 开头
    vfs_starts_with_workspace = vfs_path.startswith("workspace/") or vfs_path == "workspace"

    if base_ends_with_workspace and vfs_starts_with_workspace:
        msg = (
            f"Golden Rule violation: workspace 段重复 "
            f"(base_path={base_path!r}, vfs_path={vfs_path!r})。"
            f"请检查是否多拼了一层 workspace/。"
        )
        if GOLDEN_RULE_STRICT:
            raise ValueError(msg)
        else:
            logger.warning(msg)
            return msg
    return None


def validate_filename(name: str) -> str:
    """验证文件名是否合法，返回错误消息或空字符串表示通过"""
    if not name:
        return "文件名不能为空"
    if '/' in name:
        return "文件名不能包含 /"
    if name.startswith('-'):
        return "文件名不能以 - 开头（会被当作选项）"
    if '\x00' in name:
        return "文件名不能包含 null 字符"
    # 检查危险字符
    dangerous = '&|;$`!*?<>\\"\'#'
    for c in dangerous:
        if c in name:
            return f"文件名不能包含特殊字符: {c}"
    return ""  # 验证通过


def parse_cos_date(date_str: str) -> float:
    """解析 COS 的 LastModified 时间字符串为 Unix epoch"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return 0.0


def gen_inode(key: str) -> int:
    """从 key 生成 inode 号"""
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) % (10**8)


def get_mode_from_name(name: str) -> str:
    """根据文件名获取权限字符串"""
    if name.endswith(".sh"):
        return "-rwxr-xr-x"
    elif "." not in name:
        return "-rw-r--r--"
    else:
        ext = name.rsplit(".", 1)[-1].lower()
        if ext in ("py", "js", "ts", "cpp", "c", "h", "go", "rs", "java"):
            return "-rw-r--r--"
        return "-rw-r--r--"


def format_date(mtime: float) -> str:
    """格式化日期时间"""
    dt = datetime.fromtimestamp(mtime)
    now = datetime.now()
    if dt.year == now.year:
        return dt.strftime("%b %d %H:%M")
    else:
        return dt.strftime("%b %d  %Y")


def format_size(size: int, human: bool = False) -> str:
    """格式化文件大小"""
    if not human:
        return str(size)

    if size < 1024:
        return str(size)
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}M"
    else:
        return f"{size / (1024 * 1024 * 1024):.1f}G"


def classify_suffix(entry) -> str:
    """-F 标识符"""
    if entry.type == "directory":
        return "/"
    elif entry.type == "symlink":
        return "@"
    elif entry.name.endswith(".sh"):
        return "*"
    return ""


def mode_to_octal(mode: str) -> str:
    """权限字符串转八进制"""
    mapping = {"r": 4, "w": 2, "x": 1, "-": 0}
    result = 0
    for c in mode[1:]:  # 跳过第一个字符（文件类型）
        if c in mapping:
            result = result * 8 + mapping[c]
    return f"{result:04o}"


def mode_to_perm_string(mode: str) -> str:
    """权限字符串转 rwx 格式"""
    return mode[1:]


def parse_cut_fields(fields_str: str) -> List[Tuple[int, int]]:
    """解析字段规格，如 '1', '1-3', '1,3,5'"""
    ranges = []
    for part in fields_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            ranges.append((int(start), int(end) if end else -1))
        else:
            ranges.append((int(part), int(part)))
    return ranges


class PathResolver:
    """
    COS 路径解析器，将 VFS 虚拟路径映射为 COS key
    """

    def __init__(self, base_path: str, agent_id: Optional[str] = None, public_base_path: Optional[str] = None):
        self.base_path = base_path
        self.agent_id = agent_id
        self._public_base_path = public_base_path

    def set_public_base_path(self, public_base_path: str):
        self._public_base_path = public_base_path

    def get_public_base_path(self) -> str:
        return self._public_base_path or ""

    def resolve_path(self, path: str, cwd: str = "") -> Tuple[Optional[str], Optional[str]]:
        """
        将虚拟路径解析为 COS key

        Returns:
            (cos_key, error_msg)
        """
        if not path:
            return self.base_path, None

        if path == ".":
            if cwd:
                return f"{self.base_path}{cwd}", None
            return self.base_path, None

        path = path.strip()

        # 处理 ~ 展开为用户根目录
        if path == "~":
            path = ""
        elif path.startswith("~/"):
            path = path[2:]

        # 处理 /config/ 前缀（虚拟配置路径）
        normalized = path.lstrip("/")
        if normalized == "config" or normalized.startswith("config/"):
            return ("__CONFIG__:" + normalized, None)

        # 处理绝对路径
        if path.startswith("/"):
            path = path.lstrip("/")
            # 处理 /public/ 公共数据空间
            if path == "public" or path.startswith("public/"):
                subpath = normalized[7:] if normalized.startswith("public/") else ""
                cos_key = self.get_public_base_path() + subpath
                return (cos_key, None)
        elif cwd:
            path = f"{cwd}/{path}"

        # 处理 . 和 ..
        parts = []
        for part in path.split("/"):
            if part == "." or part == "":
                continue
            elif part == "..":
                if not parts:
                    return None, f"Error: 路径不允许 .. 穿越: {path}"
                parts.pop()
            else:
                parts.append(part)

        resolved = "/".join(parts)
        # P1.2: Golden Rule 检查 -- 检测 workspace 段重复
        check_golden_rule(self.base_path, resolved)
        cos_key = f"{self.base_path}{resolved}" if resolved else self.base_path
        return cos_key, None

    def resolve_to_dir_prefix(self, path: str, cwd: str = "") -> Tuple[str, Optional[str]]:
        """解析为目录前缀（确保以 / 结尾）"""
        cos_key, err = self.resolve_path(path, cwd)
        if err:
            return "", err
        if not cos_key.endswith("/"):
            cos_key += "/"
        return cos_key, None

    def get_cos_prefix(self, path: str = "", cwd: str = "") -> str:
        """获取 COS 前缀"""
        if not path:
            return self.base_path if not cwd else f"{self.base_path}{cwd}/"
        if path == ".":
            return self.base_path if not cwd else f"{self.base_path}{cwd}/"
        elif path.startswith("/"):
            resolved = path.lstrip("/")
            return f"{self.base_path}{resolved}/" if resolved else self.base_path
        elif path == "..":
            if not cwd:
                return self.base_path
            parent = "/".join(cwd.split("/")[:-1])
            return f"{self.base_path}{parent}/" if parent else self.base_path
        else:
            base = self.base_path if not cwd else f"{self.base_path}{cwd}/"
            return f"{base}{path}/"

    def vpath_to_cos(self, vpath: str, cwd: str = "") -> str:
        """将虚拟路径转为 COS key（文件用，不加 /）"""
        cos_key, err = self.resolve_path(vpath, cwd)
        if err:
            return ""
        return cos_key

    def is_public_path(self, cos_key: str) -> bool:
        """检查 cos_key 是否属于 /public/ 公共空间"""
        public_base = self.get_public_base_path()
        return bool(cos_key.startswith(public_base)) if public_base else False

    def vpath_to_config_key(self, vpath: str) -> str:
        """将 VFS 相对路径转为 DB config key"""
        if self.agent_id and "/" not in vpath:
            return f"agents/{self.agent_id}/{vpath}"
        return vpath

    @staticmethod
    def make_file_entry(key: str, rel_path: str, size: int, last_modified: str, etag: str):
        """
        从 COS 对象创建 FileEntry（延迟导入避免循环依赖）

        Returns:
            dict with keys: name, path, type, size, mtime, mode, inode, is_hidden, nlink
        """
        name = os.path.basename(rel_path.rstrip("/"))
        is_dir = rel_path.endswith("/")
        is_hidden = name.startswith(".") if name else False
        mtime = parse_cos_date(last_modified)

        if is_dir:
            ftype = "directory"
            perm = "drwxr-xr-x"
            nlink = 2
        else:
            ftype = "file"
            perm = get_mode_from_name(name)
            nlink = 1

        inode_val = gen_inode(f"{key}{etag}")

        return {
            "name": name,
            "path": rel_path.rstrip("/"),
            "type": ftype,
            "size": size if not is_dir else 4096,
            "mtime": mtime,
            "mode": perm,
            "inode": inode_val,
            "is_hidden": is_hidden,
            "nlink": nlink,
        }
