"""
工具注册系统（Tool Registry）
装饰器驱动，自动生成 OpenAI function calling schema + 自动 dispatch
"""
import inspect
import re
import typing
from dataclasses import dataclass, replace as _dc_replace
from typing import Dict, Any, List, Optional, Callable, FrozenSet, Mapping, Tuple, get_type_hints


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}

# T03a：工具历史别名 → 注册表标准名。**唯一权威映射**。
# 为什么放在这里：别名解析必须同时覆盖「查工具」(get_tool) 与「启用/禁用过滤」，
# 否则禁用标准名后仍可通过旧名调用（或旧配置启用旧名但模型拿不到 schema）。
# scripts/check_contracts.py 的 reminder-alias-* 规则也要求映射只存在于 registry。
TOOL_NAME_ALIASES: Dict[str, str] = {
    "schedule_reminder": "create_cron_job",
}


def normalize_tool_name(name: str) -> str:
    """把工具别名解析为注册表标准名；未知名字原样返回（不静默改成别的工具）。"""
    if not isinstance(name, str):
        return name
    return TOOL_NAME_ALIASES.get(name, name)


def tool(
    name: Optional[str] = None,
    description: str = "",
    category: str = "general",
):
    """
    工具注册装饰器

    Args:
        name: 工具名称（默认使用函数名）
        description: 工具描述
        category: 工具分类（general / file / web / code / memory / agent）

    inspect.signature 会自动提取参数名、类型（通过 type hints）、默认值
    参数描述自动从 docstring 的 ``:param name: description`` 中提取
    """
    def decorator(func: Callable) -> Callable:
        nonlocal name
        if name is None:
            name = func.__name__

        # 从 docstring 提取参数描述
        param_descriptions = _extract_param_descriptions(func)

        # 从 type hints 和 inspect 提取 schema
        sig = inspect.signature(func)
        hints = get_type_hints(func) if hasattr(func, "__annotations__") else {}

        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue

            # 提取参数类型
            param_type = hints.get(param_name, str)
            json_type = _python_type_to_json_type(param_type)

            prop = {"type": json_type}

            # 注入参数描述
            if param_name in param_descriptions:
                prop["description"] = param_descriptions[param_name]

            # 如果有默认值，写入 schema
            if param.default is not inspect.Parameter.empty:
                if isinstance(param.default, str):
                    prop["default"] = param.default
                elif isinstance(param.default, (int, float, bool)):
                    prop["default"] = param.default

            properties[param_name] = prop

            # 无默认值的参数是 required
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        # 构建 OpenAI function calling schema
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                }
            }
        }
        if required:
            schema["function"]["parameters"]["required"] = required

        # 注册到全局注册表
        TOOL_REGISTRY[name] = {
            "name": name,
            "func": func,
            "description": description,
            "category": category,
            "schema": schema,
            "param_names": list(properties.keys()),
        }

        return func

    return decorator


def _python_type_to_json_type(py_type) -> str:
    """Python 类型到 JSON Schema 类型的映射"""
    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    # 处理 Optional / Union 类型（Optional[X] 是 Union[X, None]）
    origin = getattr(py_type, "__origin__", None)
    if origin is typing.Union:
        args = getattr(py_type, "__args__", [])
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            py_type = non_none[0]

    return type_map.get(py_type, "string")


def _extract_param_descriptions(func: Callable) -> Dict[str, str]:
    """从函数 docstring 中提取 ``:param name: description`` 格式的参数描述

    Supports::

        :param name: description text
        :param type name: description text (type prefix is ignored)
    """
    doc = func.__doc__
    if not doc:
        return {}

    descriptions = {}
    # Match both ":param name: desc" and ":param type name: desc"
    for m in re.finditer(r':param\s+(?:\w+\s+)?(\w+)\s*:\s*(.+)', doc):
        descriptions[m.group(1)] = m.group(2).strip()
    return descriptions


def get_tool_schemas() -> List[Dict]:
    """获取所有工具的 OpenAI function calling schema"""
    return [entry["schema"] for entry in TOOL_REGISTRY.values()]


def get_tool(name: str) -> Optional[Dict[str, Any]]:
    """根据名称获取工具信息（先把历史别名归一化为标准名）。"""
    return TOOL_REGISTRY.get(normalize_tool_name(name))


def get_tools_by_category(category: str) -> List[Dict[str, Any]]:
    """按分类获取工具列表"""
    return [e for e in TOOL_REGISTRY.values() if e["category"] == category]


def get_all_tool_names() -> List[str]:
    """获取所有工具名称列表"""
    return list(TOOL_REGISTRY.keys())


# ══════════════════════════════════════════════════════════════════════════
# T04：执行效果 / 能力元数据（本文件是**唯一权威**）
#
# 为什么放在 registry 而不是各工具模块：
#   1. 授权判定（services/execution_policy.py）必须在“找到方法/实际调用”之前就能
#      回答“这个工具会做什么”，不能依赖工具实现里的注解；集中一张表才可被测试枚举。
#   2. 与给模型看的 OpenAI schema **刻意分开**：schema 是提示词面，本表是安全面。
#      模型不能通过参数改变 effects / required_capabilities。
#   3. 工具实现文件（services/tools/*）不因安全元数据而被改动 —— 本表只读消费。
#
# 缺省语义（fail closed）：
#   - 未登记的工具 → classification="unknown"、effects=() —— **不表示只读**，
#     而是“无法证明只读”；execution_policy 会因此拒绝执行（见工具未分类拒绝）。
#   - retry_safe / supports_idempotency_key 在 unknown 时一律 False（不鼓励自动重试）。
#
# 词表（改动需同步 execution_policy 的测试）：
#   effects: read / write / delete / execute / publish / send / schedule / delegate / system
#   capabilities: 见下方 CAP_* 常量（file.read / file.write / sandbox.execute / ...）
#
# 边界说明：
#   - `execute` 只用于“模型可控制的代码/命令执行”（bash / python_*）。
#     固定可信的转换管线（create_pptx、parse_file 内部的 subprocess）不算 execute。
#   - `system` 只用于系统托管资源（如 .logs 清理）；readonly 绝不能因 category=file
#     就放行 cleanup_logs。
# ══════════════════════════════════════════════════════════════════════════

# ── effects 词表 ────────────────────────────────────────────────────────────
EFFECT_READ = "read"
EFFECT_WRITE = "write"
EFFECT_DELETE = "delete"
EFFECT_EXECUTE = "execute"
EFFECT_PUBLISH = "publish"
EFFECT_SEND = "send"
EFFECT_SCHEDULE = "schedule"
EFFECT_DELEGATE = "delegate"
EFFECT_SYSTEM = "system"

ALL_EFFECTS: FrozenSet[str] = frozenset({
    EFFECT_READ,
    EFFECT_WRITE,
    EFFECT_DELETE,
    EFFECT_EXECUTE,
    EFFECT_PUBLISH,
    EFFECT_SEND,
    EFFECT_SCHEDULE,
    EFFECT_DELEGATE,
    EFFECT_SYSTEM,
})

# ── capabilities 词表 ──────────────────────────────────────────────────────
CAP_FILE_READ = "file.read"
CAP_FILE_WRITE = "file.write"
CAP_FILE_DELETE = "file.delete"
CAP_SANDBOX_EXECUTE = "sandbox.execute"
CAP_AGENT_DELEGATE = "agent.delegate"
CAP_AGENT_PUBLISH = "agent.publish"
CAP_SHARE_CREATE = "share.create"
CAP_GROUP_READ = "group.read"
CAP_GROUP_SEND = "group.send"
CAP_SESSION_READ = "session.read"
CAP_SESSION_WRITE = "session.write"
CAP_SCHEDULE_READ = "schedule.read"
CAP_SCHEDULE_WRITE = "schedule.write"
CAP_KNOWLEDGE_READ = "knowledge.read"
CAP_LLM_COMPUTE = "llm.compute"
CAP_MEDIA_GENERATE = "media.generate"
CAP_WEB_READ = "web.read"
CAP_SYSTEM_LOGS = "system.logs"
CAP_PERMISSION_READ = "permission.read"
CAP_PERMISSION_REQUEST = "permission.request"
CAP_REPLY_BUFFER_WRITE = "reply_buffer.write"

ALL_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_FILE_READ,
    CAP_FILE_WRITE,
    CAP_FILE_DELETE,
    CAP_SANDBOX_EXECUTE,
    CAP_AGENT_DELEGATE,
    CAP_AGENT_PUBLISH,
    CAP_SHARE_CREATE,
    CAP_GROUP_READ,
    CAP_GROUP_SEND,
    CAP_SESSION_READ,
    CAP_SESSION_WRITE,
    CAP_SCHEDULE_READ,
    CAP_SCHEDULE_WRITE,
    CAP_KNOWLEDGE_READ,
    CAP_LLM_COMPUTE,
    CAP_MEDIA_GENERATE,
    CAP_WEB_READ,
    CAP_SYSTEM_LOGS,
    CAP_PERMISSION_READ,
    CAP_PERMISSION_REQUEST,
    CAP_REPLY_BUFFER_WRITE,
})

CLASSIFICATION_DECLARED = "declared"
CLASSIFICATION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolEffectMetadata:
    """一个工具的执行元数据（不可变；授权判定的输入）。

    字段：
        name: 注册表标准名（别名已归一化）
        classification: "declared"（人工显式分类）/ "unknown"（未分类，按不可信处理）
        effects: 该工具**可能**产生的效果集合（空集合只在 declared 时才表示“无副作用”）
        required_capabilities: 执行该工具必须授予的能力
        retry_safe: 相同调用重放是否不会产生额外副作用（unknown → False）
        supports_idempotency_key: 语义上能否绑定调用方生成的幂等键（当前没有工具接收
            该参数；该标记只表示“可安全绑定”，由调用层负责生成与去重）
        registered: 该名字当前是否真的注册在 TOOL_REGISTRY（查询时计算）
    """

    name: str
    classification: str
    effects: FrozenSet[str]
    required_capabilities: FrozenSet[str]
    retry_safe: bool
    supports_idempotency_key: bool
    registered: bool


# 工具名 → (effects, required_capabilities, retry_safe, supports_idempotency_key)
# 逐个人工分类；新增工具必须在此登记，否则授权层按 unknown 拒绝执行。
_DECLARED: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...], bool, bool]] = {
    # ── 会话 / 记忆 ────────────────────────────────────────────────────────
    "auto_suggest_session": ((EFFECT_READ,), (CAP_SESSION_READ,), True, True),
    "list_conversations": ((EFFECT_READ,), (CAP_SESSION_READ,), True, True),
    "load_conversation": ((EFFECT_READ,), (CAP_SESSION_READ,), True, True),
    "search_sessions": ((EFFECT_READ,), (CAP_SESSION_READ,), True, True),
    "generate_summary": ((), (CAP_LLM_COMPUTE,), True, True),
    "end_conversation": ((EFFECT_WRITE,), (CAP_SESSION_WRITE,), False, False),
    # ── 文件 / VFS ─────────────────────────────────────────────────────────
    "file_read": ((EFFECT_READ,), (CAP_FILE_READ,), True, True),
    "file_list": ((EFFECT_READ,), (CAP_FILE_READ,), True, True),
    "file_write": ((EFFECT_WRITE,), (CAP_FILE_WRITE,), False, False),
    "edit": ((EFFECT_WRITE,), (CAP_FILE_WRITE,), False, False),
    "file_delete": ((EFFECT_DELETE,), (CAP_FILE_DELETE,), True, True),
    "parse_file": ((EFFECT_READ, EFFECT_DELEGATE), (CAP_FILE_READ, CAP_AGENT_DELEGATE), False, False),
    "create_pptx": ((EFFECT_READ, EFFECT_WRITE), (CAP_FILE_READ, CAP_FILE_WRITE), False, False),
    "resolve_share_reference": ((EFFECT_READ,), (CAP_FILE_READ,), True, True),
    # ── 文件权限 / 审批 ────────────────────────────────────────────────────
    "file_permission_ask": ((EFFECT_READ,), (CAP_PERMISSION_READ,), True, True),
    "file_permission_list": ((EFFECT_READ,), (CAP_PERMISSION_READ,), True, True),
    "request_permission": ((EFFECT_WRITE,), (CAP_PERMISSION_REQUEST,), False, False),
    # ── 系统托管资源 ───────────────────────────────────────────────────────
    "cleanup_logs": ((EFFECT_DELETE, EFFECT_SYSTEM), (CAP_SYSTEM_LOGS,), False, False),
    # ── 代码执行 ───────────────────────────────────────────────────────────
    "bash": ((EFFECT_EXECUTE, EFFECT_WRITE), (CAP_SANDBOX_EXECUTE,), False, False),
    "python_background": ((EFFECT_EXECUTE, EFFECT_WRITE), (CAP_SANDBOX_EXECUTE,), False, False),
    "python_task_list": ((EFFECT_READ,), (CAP_SANDBOX_EXECUTE,), True, True),
    "python_task_output": ((EFFECT_READ,), (CAP_SANDBOX_EXECUTE,), True, True),
    "python_task_stop": ((EFFECT_EXECUTE,), (CAP_SANDBOX_EXECUTE,), True, True),
    # ── 子 Agent ───────────────────────────────────────────────────────────
    "spawn_subagent": ((EFFECT_DELEGATE,), (CAP_AGENT_DELEGATE,), False, False),
    "read_subagent_log": ((EFFECT_READ,), (CAP_AGENT_DELEGATE,), True, True),
    # ── 定时 / cron / 文件监控 ─────────────────────────────────────────────
    "create_cron_job": ((EFFECT_WRITE, EFFECT_SCHEDULE), (CAP_SCHEDULE_WRITE,), False, False),
    "list_reminders": ((EFFECT_READ,), (CAP_SCHEDULE_READ,), True, True),
    "cancel_reminder": ((EFFECT_WRITE,), (CAP_SCHEDULE_WRITE,), True, True),
    "coprocessor_add_cron": ((EFFECT_WRITE, EFFECT_SCHEDULE), (CAP_SCHEDULE_WRITE,), False, False),
    "coprocessor_remove_cron": ((EFFECT_WRITE,), (CAP_SCHEDULE_WRITE,), True, True),
    "coprocessor_add_file_watch": ((EFFECT_WRITE, EFFECT_SCHEDULE), (CAP_SCHEDULE_WRITE,), False, False),
    "coprocessor_list": ((EFFECT_READ,), (CAP_SCHEDULE_READ,), True, True),
    # ── 群 / 发言渠道 ──────────────────────────────────────────────────────
    "get_group_history": ((EFFECT_READ,), (CAP_GROUP_READ,), True, True),
    "create_post": ((EFFECT_PUBLISH, EFFECT_SEND), (CAP_GROUP_SEND,), False, False),
    "reply_buffer_write": ((EFFECT_WRITE,), (CAP_REPLY_BUFFER_WRITE,), False, False),
    "reply_buffer_edit": ((EFFECT_WRITE,), (CAP_REPLY_BUFFER_WRITE,), False, False),
    "reply_buffer_stash": ((EFFECT_WRITE,), (CAP_REPLY_BUFFER_WRITE,), False, False),
    "reply_buffer_pop": ((EFFECT_WRITE,), (CAP_REPLY_BUFFER_WRITE,), False, False),
    "reply_buffer_cancel": ((EFFECT_WRITE,), (CAP_REPLY_BUFFER_WRITE,), True, True),
    "reply_buffer_flush": ((EFFECT_SEND,), (CAP_GROUP_SEND,), False, False),
    # ── 分享 / 发布 ────────────────────────────────────────────────────────
    "create_share_link": ((EFFECT_PUBLISH,), (CAP_SHARE_CREATE,), False, False),
    "generate_totp": ((EFFECT_PUBLISH,), (CAP_SHARE_CREATE,), False, False),
    "fe_publish": ((EFFECT_PUBLISH,), (CAP_AGENT_PUBLISH,), False, False),
    "route_register": ((EFFECT_PUBLISH,), (CAP_AGENT_PUBLISH,), False, False),
    "route_unregister": ((EFFECT_PUBLISH,), (CAP_AGENT_PUBLISH,), True, True),
    "route_list": ((EFFECT_READ,), (CAP_AGENT_PUBLISH,), True, True),
    # ── FeHub 版本控制 ─────────────────────────────────────────────────────
    "fe_init": ((EFFECT_WRITE,), (CAP_FILE_WRITE,), False, False),
    "fe_vcs_commit": ((EFFECT_WRITE,), (CAP_FILE_WRITE,), False, False),
    "fe_vcs_restore": ((EFFECT_WRITE,), (CAP_FILE_WRITE,), False, False),
    "fe_vcs_log": ((EFFECT_READ,), (CAP_FILE_READ,), True, True),
    "fe_vcs_diff": ((EFFECT_READ,), (CAP_FILE_READ,), True, True),
    # ── 待办 ───────────────────────────────────────────────────────────────
    "todo_create": ((EFFECT_WRITE,), (CAP_FILE_WRITE,), False, False),
    "todo_update": ((EFFECT_WRITE,), (CAP_FILE_WRITE,), True, True),
    "todo_complete": ((EFFECT_WRITE,), (CAP_FILE_WRITE,), True, True),
    "todo_list": ((EFFECT_READ,), (CAP_FILE_READ,), True, True),
    # ── 模型辅助计算（无持久副作用） ───────────────────────────────────────
    "text_summarize": ((), (CAP_LLM_COMPUTE,), True, True),
    "text_translate": ((), (CAP_LLM_COMPUTE,), True, True),
    "image_generate": ((), (CAP_MEDIA_GENERATE,), False, False),
    "tts": ((EFFECT_WRITE,), (CAP_MEDIA_GENERATE, CAP_FILE_WRITE), False, False),
    # ── 联网 / 知识库 ──────────────────────────────────────────────────────
    "web_fetch": ((EFFECT_READ,), (CAP_WEB_READ,), True, True),
    # allow_images=True 时会下载图片到 VFS —— 见 tool_conditional_effects()
    "web_search": ((EFFECT_READ,), (CAP_WEB_READ,), True, True),
    "knowledge_get": ((EFFECT_READ,), (CAP_KNOWLEDGE_READ,), True, True),
    "knowledge_search": ((EFFECT_READ,), (CAP_KNOWLEDGE_READ,), True, True),
}


def _build_declared_metadata() -> Dict[str, ToolEffectMetadata]:
    built: Dict[str, ToolEffectMetadata] = {}
    for tool_name, (effects, caps, retry_safe, idem_key) in _DECLARED.items():
        built[tool_name] = ToolEffectMetadata(
            name=tool_name,
            classification=CLASSIFICATION_DECLARED,
            effects=frozenset(effects),
            required_capabilities=frozenset(caps),
            retry_safe=bool(retry_safe),
            supports_idempotency_key=bool(idem_key),
            registered=True,
        )
    return built


TOOL_EFFECT_METADATA: Dict[str, ToolEffectMetadata] = _build_declared_metadata()


def _validate_declared_metadata() -> None:
    """导入时自检：效果/能力词表拼错要在启动时炸，而不是在授权时静默放行。"""
    for tool_name, meta in TOOL_EFFECT_METADATA.items():
        bad_effects = meta.effects - ALL_EFFECTS
        if bad_effects:
            raise ValueError(f"工具 {tool_name} 声明了未知 effects: {sorted(bad_effects)}")
        bad_caps = meta.required_capabilities - ALL_CAPABILITIES
        if bad_caps:
            raise ValueError(f"工具 {tool_name} 声明了未知 capabilities: {sorted(bad_caps)}")


_validate_declared_metadata()


def _unknown_metadata(tool_name: str, *, registered: bool) -> ToolEffectMetadata:
    return ToolEffectMetadata(
        name=tool_name,
        classification=CLASSIFICATION_UNKNOWN,
        effects=frozenset(),
        required_capabilities=frozenset(),
        retry_safe=False,
        supports_idempotency_key=False,
        registered=registered,
    )


def tool_metadata(name: Any) -> ToolEffectMetadata:
    """查询工具执行元数据（别名先归一化）。

    永远返回一个对象：未登记/未分类的工具返回 classification="unknown" 的元数据，
    而不是 None —— 调用方必须显式检查 classification，不能把空 effects 当“只读”。
    """
    canonical = normalize_tool_name(name)
    if not isinstance(canonical, str):
        return _unknown_metadata(str(canonical), registered=False)
    registered = canonical in TOOL_REGISTRY
    declared = TOOL_EFFECT_METADATA.get(canonical)
    if declared is None:
        return _unknown_metadata(canonical, registered=registered)
    if declared.registered == registered:
        return declared
    return _dc_replace(declared, registered=registered)


def tool_effects(name: Any) -> FrozenSet[str]:
    """工具声明的 effects 集合。空集合**不**代表只读：必须结合 tool_classification()。"""
    return tool_metadata(name).effects


def tool_capabilities(name: Any) -> FrozenSet[str]:
    """执行该工具必须具备的能力集合。"""
    return tool_metadata(name).required_capabilities


def tool_classification(name: Any) -> str:
    """"declared"（已显式分类）或 "unknown"（未分类，按不可信处理）。"""
    return tool_metadata(name).classification


def is_tool_classified(name: Any) -> bool:
    return tool_classification(name) == CLASSIFICATION_DECLARED


def is_retry_safe(name: Any) -> bool:
    """重放相同调用是否不会产生额外副作用。unknown 工具一律 False（不自动重试）。"""
    return bool(tool_metadata(name).retry_safe)


def supports_idempotency_key(name: Any) -> bool:
    """该工具语义上能否绑定调用方生成的幂等键。unknown 工具一律 False。"""
    return bool(tool_metadata(name).supports_idempotency_key)


def tool_conditional_effects(name: Any, arguments: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
    """按参数追加的 effects（静态表无法表达的少数情况）。

    规则必须保持**极短**且逐条有理由；这里不是给工具加第二套分类，而是修正
    “同一工具在不同参数下效果不同”的少数真实差异：
      - web_search(allow_images=True)：会把搜索到的图片下载进 Agent 的 VFS
        （web_tools 直写 storage.put_object），因此额外产生 write。
    """
    canonical = normalize_tool_name(name)
    extra: set = set()
    if canonical == "web_search" and isinstance(arguments, Mapping):
        allow_images = arguments.get("allow_images")
        if allow_images:
            extra.add(EFFECT_WRITE)
    return frozenset(extra)


def iter_tool_metadata() -> List[ToolEffectMetadata]:
    """按名字排序返回全部**已注册**工具的元数据（用于覆盖度自检/审计）。"""
    return [tool_metadata(tool_name) for tool_name in sorted(TOOL_REGISTRY.keys())]


def unknown_tool_names() -> List[str]:
    """已注册但未显式分类的工具名（应为空；非空时授权层会拒绝它们）。"""
    return [
        tool_name
        for tool_name in sorted(TOOL_REGISTRY.keys())
        if not is_tool_classified(tool_name)
    ]


__all__ = [
    "EFFECT_READ", "EFFECT_WRITE", "EFFECT_DELETE", "EFFECT_EXECUTE", "EFFECT_PUBLISH",
    "EFFECT_SEND", "EFFECT_SCHEDULE", "EFFECT_DELEGATE", "EFFECT_SYSTEM", "ALL_EFFECTS",
    "CAP_FILE_READ", "CAP_FILE_WRITE", "CAP_FILE_DELETE", "CAP_SANDBOX_EXECUTE",
    "CAP_AGENT_DELEGATE", "CAP_AGENT_PUBLISH", "CAP_SHARE_CREATE", "CAP_GROUP_READ",
    "CAP_GROUP_SEND", "CAP_SESSION_READ", "CAP_SESSION_WRITE", "CAP_SCHEDULE_READ",
    "CAP_SCHEDULE_WRITE", "CAP_KNOWLEDGE_READ", "CAP_LLM_COMPUTE", "CAP_MEDIA_GENERATE",
    "CAP_WEB_READ", "CAP_SYSTEM_LOGS", "CAP_PERMISSION_READ", "CAP_PERMISSION_REQUEST",
    "CAP_REPLY_BUFFER_WRITE", "ALL_CAPABILITIES",
    "CLASSIFICATION_DECLARED", "CLASSIFICATION_UNKNOWN", "ToolEffectMetadata",
    "TOOL_EFFECT_METADATA", "tool_metadata", "tool_effects", "tool_capabilities",
    "tool_classification", "is_tool_classified", "is_retry_safe",
    "supports_idempotency_key", "tool_conditional_effects", "iter_tool_metadata",
    "unknown_tool_names",
]
