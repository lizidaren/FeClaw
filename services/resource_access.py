r"""资源授权（resource access）—— **全仓唯一的“规范路径 → ResolvedResource → owner/ACL”判定**。

T04 底座切片。本模块只提供实现与测试，**不替换**现有调用点；把
`routers/*`、`services/tools/*`、`services/approval_service.py` 换到这里的入口是
后续切片的工作（见交付报告的跨界清单）。

为什么必须有这一层（审计里真实发生过的缺陷）
-------------------------------------------
1. 分享路径曾能回退到硬编码 `feclaw/user_workspaces/2/...`，跨用户读到别人文件。
2. `..` / 反斜杠 / URL 编码（`%2e%2e`、`%2f`、`%5c`）/ 大小写变体都能绕过
   `startswith` 式判断。
3. `/api/vfs/view` 在 Windows 上因 `os.path.normpath` 把 `/` 归一成 `\` 而整段跳过
   租户校验（该路由已在别处修）。
所以本模块的立场是：**先归一化，再判定；归一化失败即拒绝；绝不按存在性回退猜测身份**。

分工（本模块刻意不重复实现的东西）
---------------------------------
- Agent 归属：复用 `utils.agent_access.agent_belongs_to_user`（全仓唯一比较实现）。
- 敏感文件 / `.logs` / `/public` 默认权限：复用 `services.permission_service`
  的 `get_default_permission`（含 `identity.md`、`soul.md`、`.env`、`secret*` 等清单；
  该清单只存在于 permission_service，本模块不复制）。
- 分享映射边界与密码：复用 `services.share_service.resolve_mapping_storage_key`
  （含它自己的 fail-closed 与 `..` 拒绝），本模块只把它包成 `ResolvedResource`。
- 群共享三层空间常量：复用 `services.vfs.paths` 的 `.share/.ref/.attach` 常量。

路径归一化契约
-------------
同一逻辑文件的所有可接受写法必须得到**同一个** `canonical_path` 与 `storage_key`：

    /agents/<hash>/workspace/a/b.md
    agents/<hash>/workspace/a/b.md
    /workspace/a/b.md                （scope 已由 agent_hash 确定）
    workspace/a/b.md                 （同上）
    a/b.md                           （同上；相对路径默认落在 workspace/ 内）
        → canonical_path = /agents/<hash>/workspace/a/b.md
        → storage_key    = feclaw/agents/<hash>/workspace/a/b.md

    /mnt/group/<gid>/.share/x.md
    /groups/<gid>/.share/x.md
    groups/<gid>/.share/x.md
    .share/x.md                      （scope 已由 group_id 确定）
        → canonical_path = /groups/<gid>/.share/x.md
        → storage_key    = feclaw/groups/<gid>/.share/x.md

    /user_workspaces/<uid>/workspace/x.md
    user_workspaces/<uid>/workspace/x.md
        → canonical_path = /user_workspaces/<uid>/workspace/x.md
        → storage_key    = feclaw/user_workspaces/<uid>/workspace/x.md

    /public/x.md, public/x.md
        → canonical_path = /public/x.md
        → storage_key    = feclaw/public/x.md

明确拒绝的绕过向量（fail closed，全部抛 `ResourceAccessError`）：
    `..` 段、编码后的 `%2e%2e`/`%2f`/`%5c`/`%00`、反斜杠、NUL/控制字符、
    盘符绝对路径（`C:\\...`）、UNC/双斜杠（`//server/...`）、任何 `scheme://`
    （`file://`、`http(s)://`…）、`~` 前缀、超长路径/超长片段、空路径。

`canonical_path` 是**授权身份**形态（内部使用），不保证能被今天的 VFS/工具直接接受；
真正落存储必须只用 `storage_key`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, FrozenSet, Optional
from urllib.parse import unquote

from services.execution_policy import (
    CAPABILITY_FILE_READ,
    CAPABILITY_FILE_WRITE,
    POLICY_MODES,
    RESOURCE_SCOPES,
    SCOPE_AGENT,
    SCOPE_GROUP,
    SCOPE_PUBLIC,
    SCOPE_SHARE,
    SCOPE_USER,
    ExecutionContext,
    mode_allows_effect,
)
from services.tool_registry import EFFECT_READ, EFFECT_WRITE
from services.vfs.paths import (
    GROUP_ATTACH_DIR as _GROUP_ATTACH_DIR,
    GROUP_REF_DIR as _GROUP_REF_DIR,
    GROUP_SHARE_DIR as _GROUP_SHARE_DIR,
)

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，运行时不导入
    from sqlalchemy.orm import Session

__all__ = [
    "SCOPE_AGENT",
    "SCOPE_USER",
    "SCOPE_GROUP",
    "SCOPE_PUBLIC",
    "SCOPE_SHARE",
    "RESOURCE_SCOPES",
    "KIND_FILE",
    "KIND_DIR",
    "KIND_UNKNOWN",
    "ResourceAccessError",
    "ResolvedResource",
    "storage_prefix",
    "normalize_resource_path",
    "resolve_for_user",
    "resolve_share_mapping_resource",
    "access_reason",
    "can_read",
    "can_write",
]

# ── 资源种类 ────────────────────────────────────────────────────────────────
KIND_FILE = "file"
KIND_DIR = "dir"
KIND_UNKNOWN = "unknown"

# ── 路径形态常量 ────────────────────────────────────────────────────────────
_AGENTS_ROOT = "agents"
_USER_ROOT = "user_workspaces"
_GROUP_MOUNT_HEAD = "mnt"
_GROUP_MOUNT_TAIL = "group"
_GROUPS_ROOT = "groups"
_PUBLIC_ROOT = "public"
_WORKSPACE_DIR = "workspace"
# Agent 根目录下已知的顶层目录：相对路径以它们开头时按“Agent 根相对”解释，
# 否则按“工作区相对”解释（与 share_service.mapping_workspace_relpath 口径一致）。
_AGENT_ROOT_DIRS: FrozenSet[str] = frozenset({"workspace", "agent", ".logs"})

# agent hash：老 4 位 / 新 8 位 hex（与 utils.agent_access.HASH_MIN_LEN/HASH_MAX_LEN 一致）
_AGENT_HASH_RE = re.compile(r"^[0-9a-f]{4,8}$")
# owner id：DB 为数字，历史数据可能是字符串（与 share_service._USER_ID_RE 同一口径）
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# 群 id：UUID（String(36)）或历史别名
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

MAX_RAW_PATH_LEN = 2048
MAX_CANONICAL_PATH_LEN = 1024
MAX_SEGMENT_LEN = 255


class ResourceAccessError(Exception):
    """资源授权失败 —— 一律 fail closed。

    status_code: 400（路径/参数非法）/ 403（越权）/ 404（群等目标不存在）
    reason:      机器可读的稳定原因码（测试与路由映射都用它，不解析 detail）
    """

    def __init__(self, status_code: int, reason: str, detail: str = ""):
        self.status_code = int(status_code)
        self.reason = reason
        self.detail = detail or reason
        super().__init__(f"{reason}: {self.detail}")


@dataclass(frozen=True)
class ResolvedResource:
    """归一化后的资源身份（不可变；`storage_key` 是唯一允许用于存储调用的 key）。

    字段：
        canonical_path: 唯一规范形态（授权身份；见模块 docstring 的对照表）
        scope: agent / user / group / public / share
        agent_hash: agent 范围时的 Agent hash（hex，小写）
        user_id: user / share 范围时的 owner id（字符串）
        group_id: group 范围时的群 id
        storage_key: 规范存储 key（`feclaw/agents/<hash>/workspace/...` 等）
        kind: file / dir / unknown（normalize 阶段按尾斜杠推断；可被 storage 修正）
        exists: True/False/None（None = 未判定或读取失败；**读取失败不等于不存在**）
        owner_user_id: 由 DB 解析出的 owner（normalize 阶段为 None）
    """

    canonical_path: str
    scope: str
    agent_hash: Optional[str] = None
    user_id: Optional[str] = None
    group_id: Optional[str] = None
    storage_key: str = ""
    kind: str = KIND_UNKNOWN
    exists: Optional[bool] = None
    owner_user_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.scope not in RESOURCE_SCOPES:
            raise ResourceAccessError(400, "scope_invalid", f"未知资源范围: {self.scope!r}")
        if not isinstance(self.canonical_path, str) or not self.canonical_path.startswith("/"):
            raise ResourceAccessError(400, "path_invalid", "canonical_path 必须是绝对规范路径")
        if not isinstance(self.storage_key, str) or not self.storage_key:
            raise ResourceAccessError(400, "storage_key_invalid", "storage_key 不能为空")
        if self.kind not in (KIND_FILE, KIND_DIR, KIND_UNKNOWN):
            raise ResourceAccessError(400, "kind_invalid", f"未知 kind: {self.kind!r}")


# ── 存储前缀（唯一来源：settings.STORAGE_PREFIX） ────────────────────────────

def storage_prefix() -> str:
    """存储根前缀（含结尾 `/`），来自 `settings.STORAGE_PREFIX`（缺省 `feclaw/`）。"""
    try:
        from config import settings  # 延迟导入：纯路径归一化不该拉起整个配置栈
    except Exception:  # pragma: no cover - 配置不可用时按默认前缀，仍然可用于授权判定
        return "feclaw/"
    raw = getattr(settings, "STORAGE_PREFIX", "feclaw/") or "feclaw/"
    cleaned = str(raw).replace("\\", "/").strip("/")
    return f"{cleaned}/" if cleaned else ""


# ── 归一化 ──────────────────────────────────────────────────────────────────

def _encoded_looks_structural(value: str) -> bool:
    """解码后的串是否引入了路径结构（分隔符/`..`/控制字符）。"""
    if "/" in value or "\\" in value:
        return True
    if _CONTROL_RE.search(value):
        return True
    return ".." in value.split("/")


def _reject_encoded_traversal(raw: str) -> None:
    """拒绝“编码后变成路径结构”的写法（`%2e%2e`、`%2f`、`%5c`、`%00`…）。

    其余百分号编码（空格、UTF-8 字节等）**不解码**：canonical 保留原样，避免把
    `a%20b.md` 与 `a b.md` 两个不同存储对象折叠成同一个 key。
    """
    probe = raw
    for _ in range(3):
        decoded = unquote(probe)
        if decoded == probe:
            return
        if _encoded_looks_structural(decoded):
            raise ResourceAccessError(
                403, "path_encoded_traversal", "路径包含编码后的分隔符/上级引用"
            )
        probe = decoded


def _split_segments(text: str) -> "tuple[list, bool]":
    """切段并做逐段校验（拒绝 `..`）；返回 (segments, trailing_slash)。"""
    segments: list = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise ResourceAccessError(403, "path_traversal", "路径不允许包含 .. 段")
        if len(segment) > MAX_SEGMENT_LEN:
            raise ResourceAccessError(400, "path_segment_too_long", "路径片段过长")
        segments.append(segment)
    if not segments:
        raise ResourceAccessError(400, "path_missing", "路径没有有效片段")
    return segments, text.endswith("/")


def _check_agent_hash(value: str) -> str:
    canonical = value.lower()
    if not _AGENT_HASH_RE.match(canonical):
        raise ResourceAccessError(400, "agent_hash_invalid", f"agent hash 非法: {value!r}")
    return canonical


def _check_user_id(value: Any) -> str:
    canonical = str(value)
    if not _USER_ID_RE.match(canonical):
        raise ResourceAccessError(400, "user_id_invalid", f"user id 非法: {value!r}")
    return canonical


def _check_group_id(value: Any) -> str:
    canonical = str(value)
    if not _GROUP_ID_RE.match(canonical):
        raise ResourceAccessError(400, "group_id_invalid", f"group id 非法: {value!r}")
    return canonical


def _join(prefix: str, rest: "list[str]") -> str:
    return prefix + ("/" + "/".join(rest) if rest else "")


def _agent_relative_rest(segments: "list[str]") -> "list[str]":
    """Agent 根相对路径的解释：已知顶层目录保持原样，否则默认落在 workspace/ 内。"""
    if segments and segments[0] in _AGENT_ROOT_DIRS:
        return list(segments)
    return [_WORKSPACE_DIR] + list(segments)


def _user_relative_rest(segments: "list[str]") -> "list[str]":
    """用户工作区相对路径的解释（与 Agent 工作区一致：bare 相对路径落在 workspace/ 内）。"""
    if segments and segments[0] in (_WORKSPACE_DIR, "agent"):
        return list(segments)
    return [_WORKSPACE_DIR] + list(segments)


def normalize_resource_path(
    raw: Any,
    *,
    agent_hash: Optional[str] = None,
    user_id: Optional[str] = None,
    group_id: Optional[str] = None,
    scope_hint: Optional[str] = None,
) -> ResolvedResource:
    """把一个（不可信的）路径写法归一化为唯一 `ResolvedResource`（纯函数，不查库）。

    参数：
        raw: 待归一化的路径（来自请求参数 / 工具参数 / 模型输出 —— 一律不可信）
        agent_hash/user_id/group_id: 服务端已知的身份；用于消解相对路径，
            并**交叉校验**路径里显式写出的租户 id（不一致直接 403）
        scope_hint: 显式限定范围（agent/user/group/public）；与路径冲突 → 400

    失败抛 `ResourceAccessError`（400 非法 / 403 越权）。不返回 None，不猜测。
    """
    if not isinstance(raw, str):
        raise ResourceAccessError(400, "path_invalid", "路径必须是字符串")

    text = raw.strip()
    if not text:
        raise ResourceAccessError(400, "path_missing", "路径为空")
    if len(text) > MAX_RAW_PATH_LEN:
        raise ResourceAccessError(400, "path_too_long", "路径过长")
    if _CONTROL_RE.search(text):
        # NUL / 换行 / 其它控制字符：可用于截断或混淆路径比较
        raise ResourceAccessError(400, "path_control_char", "路径包含控制字符")
    if "\\" in text:
        # Windows 分隔符：既绕开 POSIX 段切分，也绕开 startswith 式判断
        raise ResourceAccessError(400, "path_backslash", "路径包含反斜杠")
    if _SCHEME_RE.match(text):
        raise ResourceAccessError(400, "path_scheme", "路径不能是 URL（file:// / http:// …）")
    if _DRIVE_RE.match(text):
        raise ResourceAccessError(400, "path_absolute_escape", "路径不能是盘符绝对路径")
    if text.startswith("//"):
        raise ResourceAccessError(400, "path_absolute_escape", "路径不能以 // 开头")
    _reject_encoded_traversal(text)
    if text.startswith("~"):
        raise ResourceAccessError(400, "path_tilde_unsupported", "本层不支持 ~ 展开")

    hint: Optional[str] = None
    if scope_hint is not None:
        if scope_hint not in RESOURCE_SCOPES:
            raise ResourceAccessError(400, "scope_invalid", f"未知 scope_hint: {scope_hint!r}")
        if scope_hint == SCOPE_SHARE:
            raise ResourceAccessError(
                400,
                "path_scope_share_requires_mapping",
                "分享范围必须由 share mapping 解析（见 resolve_share_mapping_resource）",
            )
        hint = scope_hint

    segments, trailing_slash = _split_segments(text)
    first = segments[0]

    explicit_agent: Optional[str] = None
    explicit_user: Optional[str] = None
    explicit_group: Optional[str] = None
    rest: "list[str]" = []

    if first == _AGENTS_ROOT:
        if len(segments) < 2:
            raise ResourceAccessError(400, "path_scope_missing", "/agents/ 需要 agent hash")
        explicit_agent = _check_agent_hash(segments[1])
        rest = segments[2:]
        scope = SCOPE_AGENT
    elif first == _USER_ROOT:
        if len(segments) < 2:
            raise ResourceAccessError(400, "path_scope_missing", "/user_workspaces/ 需要 user id")
        explicit_user = _check_user_id(segments[1])
        rest = segments[2:]
        scope = SCOPE_USER
    elif first == _GROUP_MOUNT_HEAD and len(segments) >= 2 and segments[1] == _GROUP_MOUNT_TAIL:
        if len(segments) < 3:
            raise ResourceAccessError(400, "group_id_missing", "/mnt/group/ 需要 group id")
        explicit_group = _check_group_id(segments[2])
        rest = segments[3:]
        scope = SCOPE_GROUP
    elif first == _GROUPS_ROOT:
        if len(segments) < 2:
            raise ResourceAccessError(400, "group_id_missing", "/groups/ 需要 group id")
        explicit_group = _check_group_id(segments[1])
        rest = segments[2:]
        scope = SCOPE_GROUP
    elif first == _PUBLIC_ROOT:
        rest = segments[1:]
        scope = SCOPE_PUBLIC
    else:
        # 相对路径：按 scope_hint / 服务端已知身份解释（越不确定越拒绝）。
        # hint 是权威（冲突在下面直接拒绝）；无 hint 时推断顺序固定为
        # group_id → agent_hash → user_id：
        #   * agent 是执行身份，群聊上下文里相对路径仍然落在自己的 workspace（与 VFS 一致）；
        #   * 群/用户范围只在调用方显式给出 group_id / user_id 时才被推断。
        if hint == SCOPE_USER:
            if user_id is None:
                raise ResourceAccessError(400, "path_unresolvable", "user 范围需要 user_id")
            explicit_user = _check_user_id(user_id)
            rest = _user_relative_rest(segments)
            scope = SCOPE_USER
        elif hint == SCOPE_GROUP:
            if group_id is None:
                raise ResourceAccessError(400, "path_unresolvable", "group 范围需要 group_id")
            explicit_group = _check_group_id(group_id)
            rest = list(segments)
            scope = SCOPE_GROUP
        elif hint == SCOPE_AGENT:
            if agent_hash is None:
                raise ResourceAccessError(400, "path_unresolvable", "agent 范围需要 agent_hash")
            explicit_agent = _check_agent_hash(agent_hash)
            rest = _agent_relative_rest(segments)
            scope = SCOPE_AGENT
        elif hint == SCOPE_PUBLIC:
            raise ResourceAccessError(400, "path_unresolvable", "public 路径必须显式以 /public 开头")
        elif group_id is not None:
            explicit_group = _check_group_id(group_id)
            rest = list(segments)
            scope = SCOPE_GROUP
        elif agent_hash is not None:
            explicit_agent = _check_agent_hash(agent_hash)
            rest = _agent_relative_rest(segments)
            scope = SCOPE_AGENT
        elif user_id is not None:
            explicit_user = _check_user_id(user_id)
            rest = _user_relative_rest(segments)
            scope = SCOPE_USER
        else:
            raise ResourceAccessError(
                400, "path_unresolvable", "无法确定租户：相对路径需要 agent_hash"
            )

    # scope_hint 与路径显式范围冲突 → 拒绝（不做“以 hint 为准”的猜测）
    if hint is not None and hint != scope:
        raise ResourceAccessError(
            400, "scope_conflict", f"路径范围 {scope!r} 与 scope_hint {hint!r} 冲突"
        )

    # 显式租户 id 与服务端已知身份交叉校验：不一致 = 跨租户访问
    if scope == SCOPE_AGENT and agent_hash is not None:
        if _check_agent_hash(agent_hash) != explicit_agent:
            raise ResourceAccessError(
                403, "agent_scope_mismatch", "路径指向另一个 Agent（跨 agent 访问被拒绝）"
            )
    if scope == SCOPE_USER:
        if user_id is not None and _check_user_id(user_id) != explicit_user:
            raise ResourceAccessError(403, "user_scope_mismatch", "路径指向另一个用户的工作区")
    if scope == SCOPE_GROUP and group_id is not None:
        if _check_group_id(group_id) != explicit_group:
            raise ResourceAccessError(403, "group_scope_mismatch", "路径指向另一个群")

    prefix = storage_prefix()
    if scope == SCOPE_AGENT:
        canonical_path = _join(f"/{_AGENTS_ROOT}/{explicit_agent}", rest)
        storage_key = _join(f"{prefix}{_AGENTS_ROOT}/{explicit_agent}", rest)
    elif scope == SCOPE_USER:
        canonical_path = _join(f"/{_USER_ROOT}/{explicit_user}", rest)
        storage_key = _join(f"{prefix}{_USER_ROOT}/{explicit_user}", rest)
    elif scope == SCOPE_GROUP:
        canonical_path = _join(f"/{_GROUPS_ROOT}/{explicit_group}", rest)
        storage_key = _join(f"{prefix}{_GROUPS_ROOT}/{explicit_group}", rest)
    else:  # SCOPE_PUBLIC
        canonical_path = _join(f"/{_PUBLIC_ROOT}", rest)
        storage_key = _join(f"{prefix}{_PUBLIC_ROOT}", rest)

    if len(canonical_path) > MAX_CANONICAL_PATH_LEN:
        raise ResourceAccessError(400, "path_too_long", "规范路径过长")

    kind = KIND_DIR if (trailing_slash or not rest) else KIND_FILE
    return ResolvedResource(
        canonical_path=canonical_path,
        scope=scope,
        agent_hash=explicit_agent,
        user_id=explicit_user,
        group_id=explicit_group,
        storage_key=storage_key,
        kind=kind,
        exists=None,
        owner_user_id=None,
    )


# ── DB 侧 owner / 成员判定 ──────────────────────────────────────────────────

def _assert_agent_owner(db: "Session", agent_hash: str, user_id: str) -> str:
    """Agent 归属校验（复用 utils.agent_access 的唯一比较实现）。返回 owner id。"""
    from models.agent_profile import AgentProfile
    from utils.agent_access import agent_belongs_to_user

    agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
    if not agent_belongs_to_user(agent, user_id):
        # 故意不区分“不存在”与“不属于你”，避免 hash 枚举
        raise ResourceAccessError(403, "agent_forbidden", "无权访问该 Agent 的资源")
    return str(agent.user_id)


def _assert_group_access(db: "Session", group_id: str, user_id: str, agent_hash: Optional[str]) -> str:
    """群访问校验：群主放行；否则要求“该用户拥有的 Agent 是该群成员”。返回 owner id。"""
    from models.agent_profile import AgentProfile
    from models.group import Group, GroupMember
    from utils.agent_access import agent_belongs_to_user

    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise ResourceAccessError(404, "group_not_found", "群不存在")
    owner = str(group.owner_user_id)
    if owner == str(user_id):
        return owner

    if agent_hash:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        if agent_belongs_to_user(agent, user_id):
            member = (
                db.query(GroupMember)
                .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
                .first()
            )
            if member is not None:
                return owner

    raise ResourceAccessError(403, "group_membership_required", "不是该群成员，无权访问群共享空间")


def _probe_storage(resource: ResolvedResource, storage: Any) -> ResolvedResource:
    """只读探测存在性/类型；失败保持 `exists=None`（读取失败 ≠ 不存在）。"""
    try:
        meta = storage.file_exists(resource.storage_key)
    except Exception:
        return resource
    if meta is None:
        return replace(resource, exists=False)
    exists = bool(meta.get("exists", True))
    kind = resource.kind
    if exists and meta.get("is_dir"):
        kind = KIND_DIR
    return replace(resource, exists=exists, kind=kind)


def resolve_for_user(
    db: "Session",
    user_id: Any,
    raw_path: Any,
    *,
    agent_hash: Optional[str] = None,
    group_id: Optional[str] = None,
    storage: Any = None,
) -> ResolvedResource:
    """把用户提供的路径解析成**已鉴权**的资源；越权抛 `ResourceAccessError`。

    与 `normalize_resource_path` 的区别：这里会做 owner / 群成员校验，并（可选）
    用 `storage.file_exists` 只读探测存在性。**不存在存在性回退**：路径没命中就是
    403/404，不会换个租户前缀再试一次（历史上分享路径正是这样读到别人的文件）。

    Args:
        db: SQLAlchemy 会话
        user_id: 已认证的账号 id（服务端解析，不来自请求体）
        raw_path: 不可信路径
        agent_hash: 当前 Agent 上下文（提供后路径必须与之一致）
        group_id: 当前群上下文（提供后路径必须与之一致）
        storage: 可选 FileStorage（提供后填 `exists`/`kind`；不要传可写包装）
    """
    if db is None:
        raise ResourceAccessError(500, "db_required", "resolve_for_user 需要数据库会话")
    if user_id is None or str(user_id).strip() == "":
        raise ResourceAccessError(400, "user_required", "缺少已认证用户身份")
    uid = str(user_id).strip()

    resource = normalize_resource_path(
        raw_path,
        agent_hash=agent_hash,
        user_id=uid,
        group_id=group_id,
    )

    if resource.scope == SCOPE_AGENT:
        owner = _assert_agent_owner(db, resource.agent_hash or "", uid)
        resource = replace(resource, owner_user_id=owner)
    elif resource.scope == SCOPE_USER:
        # normalize 已保证路径里的 user id == 当前用户
        resource = replace(resource, owner_user_id=resource.user_id)
    elif resource.scope == SCOPE_GROUP:
        owner = _assert_group_access(db, resource.group_id or "", uid, agent_hash)
        resource = replace(resource, owner_user_id=owner)
    elif resource.scope == SCOPE_PUBLIC:
        pass  # 平台公共只读空间：任何已认证用户可读
    else:  # pragma: no cover - normalize 不会产出 share
        raise ResourceAccessError(400, "scope_invalid", f"不支持的范围: {resource.scope!r}")

    if storage is not None:
        resource = _probe_storage(resource, storage)
    return resource


def resolve_share_mapping_resource(db: "Session", mapping: Any, *, storage: Any = None) -> ResolvedResource:
    """把已确认身份的一条分享映射解析成 `scope="share"` 的只读资源。

    边界全部复用 `services.share_service.resolve_mapping_storage_key`（它自己会拒绝
    身份缺失、owner 与 agent 冲突、跨租户路径、`..` 等），本函数只做形态转换。
    """
    from services.share_service import ShareBoundaryError, resolve_mapping_storage_key

    try:
        info = resolve_mapping_storage_key(mapping, db=db)
    except ShareBoundaryError as exc:
        raise ResourceAccessError(exc.status_code, exc.reason, exc.message) from exc

    agent_hash = (info.get("agent_hash") or "") or None
    owner = str(info.get("user_id") or "") or None
    rel = info.get("rel_path") or ""
    key = info.get("key") or ""
    if info.get("scope") == "agent":
        canonical_path = f"/agents/{agent_hash}/workspace/{rel}"
        user_id = owner
    else:
        canonical_path = f"/user_workspaces/{info.get('user_id')}/workspace/{rel}"
        agent_hash = None
        user_id = owner

    resource = ResolvedResource(
        canonical_path=canonical_path,
        scope=SCOPE_SHARE,
        agent_hash=agent_hash,
        user_id=user_id,
        group_id=None,
        storage_key=key,
        kind=KIND_FILE,
        exists=None,
        owner_user_id=owner,
    )
    if storage is not None:
        resource = _probe_storage(resource, storage)
    return resource


# ── 与 ExecutionContext 的连接 ─────────────────────────────────────────────

def _relative_for_permission(resource: ResolvedResource) -> str:
    """供 permission_service 判定的相对路径（工作区相对 / 群相对 / public 前缀）。

    必须用相对形态：permission_service 的敏感文件清单是 `identity.md`、`*/soul.md`、
    `*.env` 这类**相对**模式，喂绝对规范路径会漏判（这正是不能复制一套黑名单的原因）。
    """
    canonical = resource.canonical_path
    if resource.scope == SCOPE_AGENT:
        prefix = f"/{_AGENTS_ROOT}/{resource.agent_hash}"
    elif resource.scope == SCOPE_USER:
        prefix = f"/{_USER_ROOT}/{resource.user_id}"
    elif resource.scope == SCOPE_GROUP:
        prefix = f"/{_GROUPS_ROOT}/{resource.group_id}"
    else:
        return canonical.lstrip("/")
    if canonical == prefix:
        return ""
    return canonical[len(prefix):].lstrip("/")


def _writable_by_permission(resource: ResolvedResource, context: ExecutionContext, db: "Optional[Session]") -> bool:
    """写权限判定：复用 services.permission_service（默认清单 + 可选 DB 覆盖）。"""
    from services.permission_service import Permission, PermissionService

    rel = _relative_for_permission(resource)
    if not rel:
        return False  # 根目录不允许“写根”
    try:
        service = PermissionService(
            user_id=str(getattr(context, "user_id", "") or ""),
            db=db,
            agent_hash=(resource.agent_hash or str(getattr(context, "agent_hash", "") or "")),
        )
        permission = service.get_default_permission(rel) if db is None else service.get_permission(rel)
    except Exception:
        return False  # 判定失败一律按不可写处理
    return Permission.has_write(permission)


def access_reason(
    resource: ResolvedResource,
    context: ExecutionContext,
    *,
    action: str = EFFECT_READ,
    db: "Optional[Session]" = None,
) -> str:
    """返回 "ok" 或稳定的拒绝原因码（便于测试与审计；can_read/can_write 用它）。

    `db` 为 None 时只做策略 + 身份 + 默认权限判定；给出 db 时会额外读取
    `FilePermission` 覆盖记录（仍复用 permission_service 的查表逻辑）。
    """
    if action not in (EFFECT_READ, EFFECT_WRITE):
        raise ValueError(f"action 必须是 {EFFECT_READ!r} 或 {EFFECT_WRITE!r}")
    if not isinstance(resource, ResolvedResource):
        return "resource_invalid"

    mode = getattr(context, "policy_mode", None)
    if mode not in POLICY_MODES:
        return "policy_invalid"
    if not mode_allows_effect(mode, action):
        return "policy_denied"

    capabilities = getattr(context, "capabilities", None)
    if capabilities is None:
        return "capabilities_unset"
    required_capability = CAPABILITY_FILE_READ if action == EFFECT_READ else CAPABILITY_FILE_WRITE
    if required_capability not in capabilities:
        return "capability_missing"

    scopes = getattr(context, "allowed_resource_scopes", None)
    if scopes is None:
        return "resource_scope_unset"
    if resource.scope not in scopes:
        return "scope_not_allowed"

    context_user = str(getattr(context, "user_id", "") or "")
    context_agent = str(getattr(context, "agent_hash", "") or "")

    # 身份一致性：跨 agent / 跨用户一律不可读
    if resource.scope == SCOPE_AGENT:
        if not context_agent or resource.agent_hash != context_agent:
            return "agent_scope_mismatch"
        if resource.owner_user_id and context_user and resource.owner_user_id != context_user:
            return "owner_mismatch"
    elif resource.scope == SCOPE_USER:
        if not context_user or (resource.user_id and resource.user_id != context_user):
            return "user_scope_mismatch"
        if resource.owner_user_id and resource.owner_user_id != context_user:
            return "owner_mismatch"
    elif resource.scope == SCOPE_GROUP:
        # 群成员资格必须在 resolve_for_user（或调用方）先用 DB 确认：
        # owner_user_id 为空说明这个对象不是解析器产出的“已鉴权资源”，直接拒绝。
        if resource.owner_user_id is None:
            return "group_not_resolved"
        if not context_agent and not context_user:
            return "identity_missing"
    elif resource.scope == SCOPE_PUBLIC:
        if action == EFFECT_WRITE:
            return "public_readonly"
    elif resource.scope == SCOPE_SHARE:
        if resource.owner_user_id is None:
            return "share_not_resolved"
        if action == EFFECT_WRITE:
            return "share_readonly"
    else:
        return "scope_invalid"

    if action == EFFECT_WRITE:
        if resource.scope == SCOPE_GROUP:
            # 群三层空间：.ref/.attach 永不可写；.share 需要审批（T08），不能直写
            rel = _relative_for_permission(resource)
            head = rel.split("/", 1)[0]
            if head in (_GROUP_REF_DIR, _GROUP_ATTACH_DIR):
                return "group_readonly_scope"
            if head == _GROUP_SHARE_DIR:
                return "approval_required"
        if not _writable_by_permission(resource, context, db):
            return "file_permission_denied"

    return "ok"


def can_read(resource: ResolvedResource, context: ExecutionContext, *, db: "Optional[Session]" = None) -> bool:
    """执行上下文能否读取该资源（readonly 可读；跨租户/越权不可读）。"""
    return access_reason(resource, context, action=EFFECT_READ, db=db) == "ok"


def can_write(resource: ResolvedResource, context: ExecutionContext, *, db: "Optional[Session]" = None) -> bool:
    """执行上下文能否写该资源（readonly 恒 False；敏感文件/群只读区/公共区恒 False）。"""
    return access_reason(resource, context, action=EFFECT_WRITE, db=db) == "ok"
