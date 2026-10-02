"""
Agent 工具服务 - 基类
包含通用属性和方法：初始化、权限检查、配置管理、工具结果截断等
"""

import os
import re
import json
import uuid
import httpx
import asyncio
import logging
import nest_asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Optional

# nest_asyncio 仅在需要嵌套事件循环时按需应用（避免全局破坏 asyncio 保证）
_nest_asyncio_applied = False


def _ensure_nest_asyncio():
    """按需应用 nest_asyncio，仅在同步工具方法需要嵌套事件循环时调用"""
    global _nest_asyncio_applied
    if not _nest_asyncio_applied:
        nest_asyncio.apply()
        _nest_asyncio_applied = True

from config import settings
from services.tool_registry import tool
from models.database import SessionLocal, AgentProfile
from services.virtual_filesystem import VirtualFileSystem
from services.permission_service import PermissionService, Permission
from services.tool_log_service import TOOL_LOG_ROOT, cleanup_tool_logs

logger = logging.getLogger(__name__)


@dataclass
class Step:
    """流式步骤 dataclass"""
    step_type: str  # "pre_tool" | "tool_call" | "tool_result" | "final" | "pipeline" | "reasoning" | "keepalive" | "search_progress"
    content: str
    tool_name: Optional[str] = None
    tool_args: Optional[Dict] = None
    tool_result: Optional[str] = None  # 工具执行结果
    metadata: Optional[Dict] = None  # 额外元数据（如搜索查询标识符）
    reasoning: Optional[str] = None  # 深度思考的推理过程文本
    tool_call_id: Optional[str] = None  # 工具调用 ID（用于关联 tool_call ↔ tool_result 写入 ChatHistory）


# 默认配置
DEFAULT_CONFIG = {
    'heartbeat.interval': 900,  # 15分钟
    'session.auto_load': True,
    'session.summary_trigger_count': 50,
    'streaming': True,
    'show_tool_calls': False,
    'deep_thinking': False,
}


class AgentToolsServiceBase:
    """Agent 工具服务基类 — 通用属性和方法"""

    def __init__(self, agent_hash: str, group_id: str = None):
        """
        初始化 Agent 工具服务

        Args:
            agent_hash: Agent 的 4 位 hash
            group_id: 当前群组 ID（可选，用于 moments 发布上下文）
        """
        self.agent_hash = agent_hash
        self._user_id = None
        self._agent_profile = None
        self._group_id = group_id  # 当前群组上下文（ moments auto-publish 用）
        self.base_path = f"feclaw/agents/{self.agent_hash}/"
        self._vfs = VirtualFileSystem(agent_hash=agent_hash)
        self._storage = None
        self._perm_service = PermissionService(agent_hash)

    # ========== 属性（懒加载） ==========

    @property
    def user_id(self) -> str:
        """获取所属用户 ID（懒加载）"""
        if self._user_id is None:
            db = SessionLocal()
            try:
                agent = db.query(AgentProfile).filter(AgentProfile.hash == self.agent_hash).first()
                if agent:
                    self._user_id = str(agent.user_id)
                    self._agent_profile = agent
                else:
                    raise ValueError(f"Agent {self.agent_hash} not found")
            finally:
                db.close()
        return self._user_id

    @property
    def agent_profile(self) -> AgentProfile:
        """获取 AgentProfile（懒加载）"""
        if self._agent_profile is None:
            db = SessionLocal()
            try:
                self._agent_profile = db.query(AgentProfile).filter(AgentProfile.hash == self.agent_hash).first()
                if not self._agent_profile:
                    raise ValueError(f"Agent {self.agent_hash} not found")
            finally:
                db.close()
        return self._agent_profile

    @property
    def storage(self):
        """懒加载文件存储后端 —— 感知 STORAGE_MODE（cos / local / auto）

        local 模式下返回 LocalStorage，图片下载写 VFS 等不再因硬绑 COS 而必然失败。
        """
        if self._storage is None:
            from services.file_storage import create_file_storage
            self._storage = create_file_storage(mode=getattr(settings, "STORAGE_MODE", "auto"))
        return self._storage

    @property
    def vfs(self) -> VirtualFileSystem:
        """公开 VFS 实例（供外部服务引用）"""
        return self._vfs

    # ========== 权限检查 ==========

    def _check_read(self, path: str) -> bool:
        """检查读权限"""
        return self._perm_service.check_permission(path, Permission.READ)

    def _check_write(self, path: str) -> bool:
        """检查写权限"""
        return self._perm_service.check_permission(path, Permission.WRITE)

    # ========== 路径解析 ==========

    def _resolve(self, path: str) -> str:
        """
        将相对路径解析为 COS key
        path: workspace/, agent/, workspace/subdir/, workspace/subdir/file.txt 等
        """
        if ".." in path:
            return None
        normalized = path.strip("/")
        return f"{self.base_path}{normalized}"

    def _list_dir(self, cos_prefix: str) -> List[str]:
        """列出 COS prefix 下的直接子项（文件和一级目录）"""
        try:
            objects = self.storage.list_objects(cos_prefix)
            if not objects:
                return []
            names = set()
            for obj in objects:
                key = obj["Key"]
                rel_path = key[len(cos_prefix):].lstrip("/")
                if "/" in rel_path:
                    names.add(rel_path.split("/")[0] + "/")
                else:
                    names.add(rel_path)
            return sorted(names)
        except Exception as e:
            logger.warning(f"Failed to list directory {cos_prefix}: {e}")
            return []

    # ========== 工具结果截断（P0-Tool-Result-Budget） ==========

    # 工具结果截断阈值配置
    TOOL_RESULT_MAX_SIZE = 50000      # 50KB - 超过此大小保存到 VFS
    TOOL_RESULT_PREVIEW_SIZE = 2000   # 2KB - 保留在上下文中的预览大小

    async def _log_tool_result(
        self,
        tool_name: str,
        result: str,
        call_id: str = None,
    ) -> str:
        """将完整工具结果写入系统管理的只读 VFS 日志并返回相对路径。"""
        now = datetime.now()
        safe_tool = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name or "unknown")
        safe_tool = safe_tool.strip("._-")[:80] or "unknown"
        raw_id = call_id or uuid.uuid4().hex
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw_id))
        safe_id = safe_id.strip("._-")[:128] or uuid.uuid4().hex
        log_path = (
            f"{TOOL_LOG_ROOT}/{now.strftime('%Y-%m-%d')}/"
            f"{safe_tool}/{safe_id}.log"
        )

        try:
            write_result = await asyncio.to_thread(
                self._vfs.write_tool_log,
                log_path,
                result,
            )
            if write_result.startswith("OK"):
                return log_path
            logger.warning(
                "[ToolLogs] Failed to persist tool=%s call_id=%s: %s",
                tool_name,
                call_id,
                write_result,
            )
        except Exception as e:
            logger.warning(
                "[ToolLogs] Failed to persist tool=%s call_id=%s: %s",
                tool_name,
                call_id,
                e,
            )
        return ""

    async def _truncate_tool_result(
        self,
        result: str,
        tool_name: str = "",
        tool_args: Dict = None,
        call_id: str = None
    ) -> str:
        """双写工具结果；仅在超过 50KB 时缩减返回给上下文的内容。"""
        if result is None:
            result = ""
        if not isinstance(result, str):
            result = str(result)

        result_bytes = len(result.encode("utf-8"))
        log_path = await self._log_tool_result(
            tool_name=tool_name,
            result=result,
            call_id=call_id,
        )

        if result_bytes <= self.TOOL_RESULT_MAX_SIZE:
            return result

        logger.info(
            "[P0-Tool-Result-Budget] 工具 %s 结果 %s bytes > %s，开始截断",
            tool_name,
            result_bytes,
            self.TOOL_RESULT_MAX_SIZE,
        )

        preview = result[:self.TOOL_RESULT_PREVIEW_SIZE]
        while len(preview.encode("utf-8")) > self.TOOL_RESULT_PREVIEW_SIZE:
            preview = preview[:-1]

        if log_path:
            hint = (
                f"\n\n---\n[结果超过 50KB ({result_bytes} bytes)，已保存到 VFS: "
                f"`{log_path}`，此处仅显示前 2KB 预览]\n---\n\n"
            )
        else:
            hint = (
                "\n\n---\n[结果超过 50KB，尝试保存到 VFS 失败，"
                "此处仅显示前 2KB 预览]\n---\n\n"
            )
        return preview + hint

    @tool(
        description="清理当前 Agent 的工具调用日志。默认删除 7 天前的日志；可减小 retention_days 提前清理。",
        category="file",
    )
    def cleanup_logs(self, retention_days: int = 7) -> str:
        """手动清理当前 Agent 的过期工具调用日志。"""
        if retention_days < 0 or retention_days > 365:
            return "Error: retention_days 必须在 0 到 365 之间"
        try:
            result = cleanup_tool_logs(
                agent_hash=self.agent_hash,
                retention_days=retention_days,
                storage=self._vfs.storage,
            )
            return (
                "OK: 工具日志清理完成，"
                f"扫描 {result['scanned']} 个，删除 {result['deleted']} 个，"
                f"失败 {result['failed']} 个，截止日期 {result['cutoff_date']}"
            )
        except Exception as e:
            logger.error(f"[ToolLogs] Manual cleanup failed: {e}")
            return f"Error: 工具日志清理失败: {e}"

    # ========== 配置管理 ==========

    def get_config(self, key: str, channel: str = None) -> Any:
        """
        读取配置。支持层级 key 如 "global/streaming" 或简单 key "streaming"。

        查询优先级（sandbox 模式下）：
        1. agents/{agent_hash}/{key}    （Agent 私有）
        2. {key}                        （直接 key）
        3. global/{key}                 （全局）
        4. channels/{channel}/{key}     （渠道，如果有 channel 参数）
        5. config.global.{key}          （旧格式兜底）
        6. config.{channel}.{key}       （旧格式渠道兜底）
        7. DEFAULT_CONFIG               （代码 fallback）
        """
        from models.database import AgentConfig, SessionLocal

        search_keys = []

        if self.agent_hash:
            search_keys.append(f"agents/{self.agent_hash}/{key}")
        search_keys.append(key)
        if channel:
            search_keys.append(f"channels/{channel}/{key}")
        search_keys.append(f"global/{key}")
        if channel:
            search_keys.append(f"config.{channel}.{key}")
        search_keys.append(f"config.global.{key}")

        db = SessionLocal()
        try:
            for search_key in search_keys:
                cfg = db.query(AgentConfig).filter(
                    AgentConfig.key == search_key,
                    AgentConfig.permission != "none",
                ).first()
                if cfg and cfg.value:
                    return self._parse_config_value(cfg.value, key)
            return DEFAULT_CONFIG.get(key)
        except Exception as e:
            logger.error(f"get_config DB error: {e}")
            return DEFAULT_CONFIG.get(key)
        finally:
            db.close()

    def set_config(self, key: str, value: Any, channel: str = None, description: str = None) -> str:
        """写入配置"""
        from models.database import AgentConfig, SessionLocal

        if channel:
            config_key = f"channels/{channel}/{key}"
        elif self.agent_hash:
            config_key = f"agents/{self.agent_hash}/{key}"
        else:
            config_key = f"global/{key}"

        value_str = str(value) if not isinstance(value, str) else value

        db = SessionLocal()
        try:
            config = db.query(AgentConfig).filter(
                AgentConfig.key == config_key,
                AgentConfig.agent_hash == self.agent_hash
            ).first()
            if config:
                config.value = value_str
                config.updated_at = datetime.utcnow()
                if description:
                    config.description = description
            else:
                config = AgentConfig(
                    key=config_key,
                    value=value_str,
                    agent_hash=self.agent_hash,
                    channel=channel,
                    description=description
                )
                db.add(config)
            db.commit()
            channel_str = f"渠道[{channel}]" if channel else "全局"
            return f"OK: 已设置 {channel_str} 配置 {key}={value}"
        except Exception as e:
            return f"Error: 设置配置失败: {e}"
        finally:
            db.close()

    def get_effective_config(self, channel: str = None) -> dict:
        """获取有效配置（合并默认值 + 全局 + agent + 渠道配置）"""
        from models.database import AgentConfig, SessionLocal

        result = dict(DEFAULT_CONFIG)

        db = SessionLocal()
        try:
            null_global = db.query(AgentConfig).filter(
                AgentConfig.key.like("global/%"),
                AgentConfig.agent_hash == None
            ).all()
            for cfg in null_global:
                original_key = cfg.key.replace("global/", "")
                if cfg.value:
                    result[original_key] = self._parse_config_value(cfg.value, original_key)

            old_global = db.query(AgentConfig).filter(
                AgentConfig.key.like("config.global.%"),
                AgentConfig.agent_hash == None
            ).all()
            for cfg in old_global:
                original_key = cfg.key.replace("config.global.", "")
                if cfg.value and original_key not in result:
                    result[original_key] = self._parse_config_value(cfg.value, original_key)

            if self.agent_hash:
                prefix = f"agents/{self.agent_hash}/"
                agent_configs = db.query(AgentConfig).filter(
                    AgentConfig.key.like(f"{prefix}%"),
                    AgentConfig.agent_hash == self.agent_hash
                ).all()
                for cfg in agent_configs:
                    original_key = cfg.key.replace(prefix, "")
                    if cfg.value:
                        result[original_key] = self._parse_config_value(cfg.value, original_key)

            if channel:
                channel_prefix = f"channels/{channel}/"
                channel_configs = db.query(AgentConfig).filter(
                    AgentConfig.key.like(f"{channel_prefix}%"),
                ).all()
                for cfg in channel_configs:
                    original_key = cfg.key.replace(channel_prefix, "")
                    if cfg.value:
                        result[original_key] = self._parse_config_value(cfg.value, original_key)

            return result
        except Exception as e:
            logger.error(f"get_effective_config DB error: {e}")
            return dict(DEFAULT_CONFIG)
        finally:
            db.close()

    def _parse_config_value(self, value_str: str, key: str) -> Any:
        """解析配置值字符串，根据默认值类型推断目标类型"""
        default_value = DEFAULT_CONFIG.get(key)

        if default_value is None:
            return value_str

        if isinstance(default_value, bool):
            return value_str.lower() in ("true", "1", "yes")

        if isinstance(default_value, int):
            try:
                return int(value_str)
            except ValueError:
                return default_value

        if isinstance(default_value, float):
            try:
                return float(value_str)
            except ValueError:
                return default_value

        return value_str

    # ========== 配置工具（旧版） ==========

    def config_read(self, path: str) -> str:
        """读取配置项"""
        key = path.strip("/").replace("/", ".")
        result = self.get_config(key)
        if result is not None:
            return str(result)
        return f"Error: 配置项不存在: {key}"

    def config_write(self, path: str, value: str) -> str:
        """写入配置项"""
        key = path.strip("/").replace("/", ".")
        return self.set_config(key, value)

    def config_list(self) -> str:
        """列出所有配置项"""
        try:
            from models.database import AgentConfig, SessionLocal

            db = SessionLocal()
            try:
                q = db.query(AgentConfig).filter(AgentConfig.permission != "none")
                if self.agent_hash:
                    q = q.filter(AgentConfig.agent_hash == self.agent_hash)
                configs = q.all()
                if not configs:
                    return "（无配置项）"
                lines = []
                for c in configs:
                    lines.append(f"{c.key}={c.value}")
                return "\n".join(lines)
            finally:
                db.close()
        except Exception as e:
            return f"Error: 列出配置失败: {e}"

    def _get_config(self, key: str, default: str = "") -> str:
        """获取配置项（按 agent_hash+key 过滤）"""
        from models.database import AgentConfig, SessionLocal
        db = SessionLocal()
        try:
            q = db.query(AgentConfig).filter(AgentConfig.key == key)
            if self.agent_hash:
                q = q.filter(AgentConfig.agent_hash == self.agent_hash)
            config = q.first()
            return config.value if config else default
        finally:
            db.close()

    # ========== 平台公共文档加载 ==========

    def load_public_docs(self, doc_name: str = "index.md") -> str:
        """
        从 /public/feclaw/ 加载公共文档（平台信息，不含人格设定）
        """
        try:
            path = f"/public/feclaw/{doc_name}"
            content = self._vfs.cat(path)
            if content and not content.startswith("[Error") and not content.startswith("Error"):
                logger.info(f"[AgentTools] Loaded public doc: {path} ({len(content)} chars)")
                return content
        except Exception as e:
            logger.warning(f"[AgentTools] load_public_docs failed: {e}")
        return ""

    # ========== System Prompt 构建 ==========

    def build_system_prompt(self, soul_content: str = "", identity_content: str = "",
                             user_content: str = "", memory_content: str = "",
                             include_platform_info: bool = True) -> str:
        """
        构建完整的 system prompt。

        层次结构：
        1. 平台信息层（来自 /public/feclaw/index.md）
        2. 人格设定层（来自 SOUL.md）
        3. 身份配置层（来自 IDENTITY.md）
        4. 用户信息层（来自 USER.md）
        5. 长期记忆层（来自 memory/）
        """
        parts = []

        if include_platform_info:
            platform_info = self.load_public_docs("index.md")
            if platform_info:
                parts.append(f"【平台信息】\n{platform_info}")

        if soul_content:
            parts.append(f"【人格设定】\n{soul_content}")

        if identity_content:
            parts.append(f"【身份配置】\n{identity_content}")

        if user_content:
            parts.append(f"【用户信息】\n{user_content}")

        if memory_content:
            parts.append(f"【长期记忆】\n{memory_content}")

        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        default_hint = f"""【重要：工具调用原则】
- 当需要获取信息或执行操作时，必须真正调用工具，不要编造或复用历史结果。
- 历史消息中带有 [⚠️ 历史工具调用结果/历史错误信息] 标记的内容可能已过时，请重新调用工具确认当前状态。
- 即使历史中某个工具曾报错，也请再次尝试调用，因为问题可能已修复。

【当前时间】
{current_time}

在调用涉及时效性的工具（如 generate_totp）前，请先检查历史结果是否已过期。如果历史结果的时间戳早于当前时间，请重新调用工具。"""

        if parts:
            return "\n\n".join(parts) + "\n\n" + default_hint
        else:
            return default_hint

    # ========== Agent 间通信 ==========

    def return_tool(self, path: str, message: str) -> str:
        """
        Sub-agent 返回结果（用于 Agent 间通信）
        """
        result = self.file_write(path, message)
        return f"OK: 结果已返回到 {path}"
