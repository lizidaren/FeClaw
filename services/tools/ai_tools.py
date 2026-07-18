from __future__ import annotations

"""
Agent 工具服务 - AI/子Agent 工具
包含 spawn_subagent, text_summarize, text_translate, image_generate 等
"""

import os
import re
import json
import time
import base64
import asyncio
import hashlib
import logging
import traceback
from datetime import datetime
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor

import httpx

from config import settings
from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase

logger = logging.getLogger(__name__)


class AIToolsMixin(AgentToolsServiceBase):
    """AI/子Agent 工具 Mixin"""

    # ========== 文本工具（P2）==========

    @tool(description="对长文本生成简洁摘要。传入需要总结的文本，返回精简后的摘要。", category="code")
    def text_summarize(self, content: str) -> str:
        """
        对长文本生成简洁摘要，使用 DeepSeek。

        Args:
            content: 要总结的文本

        Returns:
            摘要文本
        """
        if not settings.DEEPSEEK_API_KEY:
            return "Error: DEEPSEEK_API_KEY 未配置"

        # 限制输入长度
        if len(content) > 5000:
            content = content[:5000] + "..."

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": settings.MAIN_TEXT_MODEL,
                        "messages": [
                            {"role": "system", "content": "你是一个文本摘要助手。请对用户提供的文本生成简洁、准确的摘要。保留关键信息和要点，去除冗余内容。摘要应简明扼要。"},
                            {"role": "user", "content": f"请总结以下文本：\n\n{content}"}
                        ]
                    }
                )
                response.raise_for_status()
                result = response.json()
                summary = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                return summary.strip() or "(生成摘要失败)"
        except Exception as e:
            logger.error(f"[text_summarize] 摘要失败: {e}")
            return f"Error: 摘要失败: {e}"

    @tool(description="翻译文本到目标语言。默认翻译为中文。", category="code")
    def text_translate(self, content: str, target_language: Optional[str] = "中文") -> str:
        """
        翻译文本到目标语言，使用 DeepSeek。

        Args:
            content: 要翻译的文本
            target_language: 目标语言，默认"中文"

        Returns:
            翻译后的文本
        """
        if not settings.DEEPSEEK_API_KEY:
            return "Error: DEEPSEEK_API_KEY 未配置"

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": settings.MAIN_TEXT_MODEL,
                        "messages": [
                            {"role": "system", "content": f"你是一个翻译助手。请将用户提供的文本翻译为{target_language}。只返回翻译结果，不要添加任何解释。"},
                            {"role": "user", "content": content}
                        ]
                    }
                )
                response.raise_for_status()
                result = response.json()
                translated = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                return translated.strip() or "(翻译失败)"
        except Exception as e:
            logger.error(f"[text_translate] 翻译失败: {e}")
            return f"Error: 翻译失败: {e}"

    @tool(description="根据提示词生成图片（文生图）。使用火山引擎 Doubao Seedream 5.0 模型。", category="code")
    def image_generate(self, prompt: str, size: Optional[str] = "2K") -> str:
        """
        根据提示词生成图片，使用火山引擎 Doubao Seedream 5.0。

        Args:
            prompt: 图片生成提示词
            size: 图片尺寸，默认"2K"

        Returns:
            生成的图片 URL
        """
        if not settings.DOUBAO_API_KEY:
            return "Error: DOUBAO_API_KEY 未配置"

        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    "https://ark.cn-beijing.volces.com/api/v3/images/generations",
                    headers={
                        "Authorization": f"Bearer {settings.DOUBAO_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": settings.DOUBAO_SEEDREAM_MODEL,
                        "prompt": prompt,
                        "size": size,
                        "response_format": "url",
                        "watermark": False
                    }
                )
                response.raise_for_status()
                result = response.json()
                images = result.get("data", [])
                if images and images[0].get("url"):
                    return images[0]["url"]
                return f"Error: 未获取到图片 URL，响应: {json.dumps(result)}"
        except Exception as e:
            logger.error(f"[image_generate] 生成失败: {e}")
            return f"Error: 图片生成失败: {e}"

    # ========== 子Agent 角色列表（已禁用 2026-05-27） ==========

    # @tool(description="...", category="agent")
    # def list_subagent_roles(self) -> str:
    #     pass

    # ========== 模式切换 ==========

    @tool(description="切换 Agent 工作模式", category="agent")
    # ========== 权限工具 ==========

    @tool(description="查询文件权限状态", category="agent")
    def file_permission_ask(self, path: str, reason: str = "") -> str:
        """
        Agent 请求文件权限（告知用户需要什么权限）

        Args:
            path: 文件路径
            reason: 请求权限的原因

        Returns:
            权限请求信息
        """
        current_perm = self._perm_service.get_permission(path)
        default_perm = self._perm_service.get_default_permission(path)

        if current_perm != default_perm:
            return f"权限信息: 文件 {path} 当前权限为 {current_perm}（默认: {default_perm}）。如需修改权限，请使用 file_permission_grant。"

        return f"权限请求: Agent 需要访问 {path}。当前默认权限: {default_perm}。原因: {reason}"

    @tool(description="列出所有文件权限设置", category="agent")
    def file_permission_list(self) -> str:
        """
        列出当前用户的所有文件权限设置

        Returns:
            权限列表
        """
        perms = self._perm_service.list_permissions()
        if not perms:
            return "（无自定义权限设置，所有文件使用默认权限 readwrite）"

        lines = []
        for p in perms:
            lines.append(f"{p.file_path}: {p.permission}")

        return "\n".join(lines)

    # ========== Sub-agent 输出摘要功能 ==========

    def _summarize_subagent_output(
        self,
        task: str,
        output: str,
    ) -> dict:
        """
        对子 Agent 的超长输出生成结构化摘要

        Args:
            task: 原始任务描述
            output: 子 Agent 的完整输出

        Returns:
            包含结构化摘要的字典:
            - task_completed: bool
            - key_results: str (最多500字)
            - files_created: list[str]
            - files_modified: list[str]
            - failure_reason: str | None
        """
        from openai import OpenAI

        summary_prompt = f"""请分析以下子 Agent 任务输出，生成结构化摘要。

【原始任务】
{task[:500]}

【子 Agent 完整输出】（共 {len(output)} 字符）
{output}

【要求】
请严格按照以下 JSON 格式输出摘要（不要输出任何其他内容，只输出 JSON）：
{{
    "task_completed": true或false,
    "key_results": "关键结果摘要（最多500字，突出核心发现和结论）",
    "files_created": ["创建的文件路径列表"],
    "files_modified": ["修改的文件路径列表"],
    "failure_reason": "如果任务失败，说明失败原因；成功则为 null"
}}

注意事项：
1. task_completed: 根据输出判断任务是否成功完成
2. key_results: 精炼总结，最多500字
3. files_created/modified: 从输出中提取实际操作的文件路径
4. failure_reason: 仅在失败时填写，成功时为 null"""

        try:
            client = OpenAI(
                api_key=settings.DOUBAO_API_KEY,
                base_url="https://ark.cn-beijing.volces.com/api/v3/",
                timeout=30.0
            )

            response = client.chat.completions.create(
                model="doubao-seed-2-0-lite-260215",
                messages=[
                    {"role": "system", "content": "你是一个摘要助手，擅长从长文本中提取关键信息并生成结构化JSON输出。只输出JSON，不要输出其他内容。"},
                    {"role": "user", "content": summary_prompt}
                ],
                stream=False
            )

            raw_content = response.choices[0].message.content.strip()

            json_str = raw_content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            summary_data = json.loads(json_str)

            result = {
                "task_completed": summary_data.get("task_completed", True),
                "key_results": summary_data.get("key_results", "")[:500],
                "files_created": summary_data.get("files_created", []),
                "files_modified": summary_data.get("files_modified", []),
                "failure_reason": summary_data.get("failure_reason")
            }

            logger.info(f"[spawn_subagent] Structured summary generated: {len(output)} chars -> summary")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"[spawn_subagent] JSON parse failed: {e}, raw: {raw_content[:200]}")
            return {
                "task_completed": True,
                "key_results": output[:500],
                "files_created": [],
                "files_modified": [],
                "failure_reason": None
            }
        except Exception as e:
            logger.error(f"[spawn_subagent] Summary generation failed: {e}")
            return {
                "task_completed": True,
                "key_results": output[:500],
                "files_created": [],
                "files_modified": [],
                "failure_reason": None
            }

    def _format_summary_output(self, summary: dict, original_length: int, log_path: str = None) -> str:
        """
        格式化结构化摘要为可读文本

        Args:
            summary: 结构化摘要字典
            original_length: 原始输出长度
            log_path: VFS 日志文件路径（可选）

        Returns:
            格式化的摘要文本
        """
        status_icon = "✅" if summary["task_completed"] else "❌"

        result = f"📋 **输出摘要**（原始输出 {original_length} 字符，已精简）\n\n"
        result += f"{status_icon} **任务状态**: {'完成' if summary['task_completed'] else '未完成'}\n\n"

        if summary["key_results"]:
            result += f"📝 **关键结果**:\n{summary['key_results']}\n\n"

        if summary["files_created"]:
            result += f"📁 **创建的文件**:\n"
            for f in summary["files_created"]:
                result += f"  + {f}\n"
            result += "\n"

        if summary["files_modified"]:
            result += f"✏️ **修改的文件**:\n"
            for f in summary["files_modified"]:
                result += f"  ~ {f}\n"
            result += "\n"

        if summary["failure_reason"]:
            result += f"⚠️ **失败原因**: {summary['failure_reason']}\n\n"

        if log_path:
            result += f"---\n💡 提示: 完整输出已保存到 `{log_path}`，可使用 `read_subagent_log` 工具查看。"
        else:
            result += f"---\n💡 提示: 原始输出已自动摘要以节省上下文空间。"

        return result

    # ========== Vision Sub-agent 工具 ==========

    @tool(description="启动子Agent处理任务。通用问题解决工具，支持文本和图片输入。model支持\"auto\"智能推荐。详见 /public/feclaw/tools/spawn_subagent.md", category="agent")
    def spawn_subagent(
        self,
        model: str,
        reasoning_effort: str,
        task: str,
        image_base64: Optional[str] = None,
        image_path: Optional[str] = None,
        custom_system_prompt: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        max_retries: int = 0,
        include_stats: bool = False,
        summarize_output: bool = False,
        preset_role: Optional[str] = None,  # 已弃用 2026-05-27
    ) -> str:
        """
        启动子Agent处理任务（支持视觉分析和通用任务）

        Args:
            model: 模型名称
            reasoning_effort: 深度思考强度
            task: 任务描述
            image_base64: 图片Base64编码（可选）
            image_path: VFS图片路径（可选）
            custom_system_prompt: 自定义system prompt（可选）
            preset_role: 已弃用，保留兼容
            timeout_seconds: 超时时间（秒），默认 600 秒
            max_retries: 最大重试次数，默认 0
            include_stats: 是否在返回结果中包含执行统计信息，默认 False
            summarize_output: 是否对超长输出自动生成摘要，默认 False

        Returns:
            子Agent的处理结果
        """
        model_recommendation_reason = None
        if model == "auto" or model is None or model == "":
            model = "qwen3.6-35b-a3b"
            reasoning_effort = None

        if reasoning_effort in ("off", "none", "disabled", ""):
            reasoning_effort = None
        elif reasoning_effort == "high":
            pass
        elif reasoning_effort == "medium":
            pass
        elif reasoning_effort == "low":
            pass
        elif reasoning_effort is None:
            pass
        else:
            reasoning_effort = None

        actual_timeout = timeout_seconds if timeout_seconds else 600

        start_time = time.time()
        call_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.agent_hash}"

        logger.info(f"[spawn_subagent] START - call_id={call_id}, model={model}, timeout={actual_timeout}s")
        logger.info(f"[spawn_subagent] START - model={model}, reasoning_effort={reasoning_effort}")

        debug_log_path = "/tmp/spawn_subagent_debug.txt"
        try:
            if os.path.exists(debug_log_path):
                file_size = os.path.getsize(debug_log_path)
                if file_size > 1024 * 1024:
                    os.remove(debug_log_path)

            with open(debug_log_path, "a") as f:
                f.write(f"\n=== {datetime.now().isoformat()} [{call_id}] ===\n")
                f.write(f"model={model}, reasoning_effort={reasoning_effort}\n")
                f.write(f"task={task[:100]}...\n")
                f.write(f"image_path={image_path}, image_base64={image_base64 is not None}\n")
        except Exception as e:
            logger.debug(f"[Subagent] Debug trace dump failed: {e}")

        logger.debug("\n\n\n\n启动subagent！！！！！！")
        logger.debug("=== CALL STACK ===")
        for line in traceback.format_stack():
            logger.debug(line.strip())
        logger.debug("==================")

        if image_path and not image_base64:
            normalized = image_path if image_path.startswith("/") else f"/{image_path}"
            cos_key = f"{settings.STORAGE_PREFIX}agents/{self.agent_hash}{normalized}"
            try:
                from services.storage_service import StorageService
                storage = StorageService()
                content = storage.get_file_content(key=cos_key)
                if not content:
                    return f"Error: 文件不存在或读取失败 {image_path}"
                image_base64 = base64.b64encode(content).decode("utf-8")
            except Exception as e:
                return f"Error: 读取图片失败 {image_path}: {e}"

        if image_base64:
            content = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_base64}"
                    }
                },
                {
                    "type": "text",
                    "text": task
                }
            ]
        else:
            content = task

        if preset_role:
            pass  # preset_role 已禁用

        if custom_system_prompt:
            if len(custom_system_prompt) > 2000:
                return f"Error: custom_system_prompt 长度超过限制（最大 2000 字符，当前 {len(custom_system_prompt)} 字符）"
            messages = [
                {"role": "system", "content": custom_system_prompt},
                {"role": "user", "content": content}
            ]
        else:
            # 极简 system prompt，仅让模型知道要做事，内容完全由主模型 task 决定
            messages = [
                {"role": "system", "content": "按照用户要求完成任务，直接输出结果。"},
                {"role": "user", "content": content}
            ]

        _SUBAGENT_PROVIDER_MAP = {
            "doubao": "doubao",
            "qwen": "qwen",
            "glm": "zhipuai",
            "kimi": "kimi",
            "deepseek": "deepseek",
        }
        model_prefix = model.split("-")[0] if model else ""
        subagent_provider = "doubao"
        for prefix, provider in _SUBAGENT_PROVIDER_MAP.items():
            if model_prefix.startswith(prefix):
                subagent_provider = provider
                break

        def _run_subagent_in_thread():
            """使用 AgentExecutor 执行 subagent 任务（支持工具调用）"""
            from services.agent_executor import AgentExecutor, SUBAGENT_BLOCKED_TOOLS
            # 延迟导入避免循环依赖
            from services.tools import AgentToolsService

            logger.debug(f"[SPAWN_SUBAGENT] START - model={model}, provider={subagent_provider}, reasoning_effort={reasoning_effort}")
            logger.debug(f"[SPAWN_SUBAGENT] messages count={len(messages)}")
            if image_base64:
                logger.debug(f"[SPAWN_SUBAGENT] image_base64 length={len(image_base64)}")

            subagent_tools = AgentToolsService(self.agent_hash)
            executor = AgentExecutor(
                self.agent_hash,
                subagent_tools,
                blocked_tools=SUBAGENT_BLOCKED_TOOLS,
            )

            async def _run():
                result = ""
                async for step in executor.chat_with_tools(
                    messages=messages,
                    provider=subagent_provider,
                    model=model,
                    reasoning_effort=reasoning_effort,
                ):
                    if step.step_type == "token":
                        result += step.content
                    elif step.step_type == "error":
                        raise Exception(step.content)
                return result

            try:
                content = asyncio.run(_run())
                logger.info(f"[SPAWN_SUBAGENT] SUCCESS - response length={len(content)}")
                return content
            except Exception as e:
                logger.error(f"[SPAWN_SUBAGENT] AgentExecutor Error: {type(e).__name__}: {e}", exc_info=True)
                raise

        last_error = None
        result_content = None
        for attempt in range(max_retries + 1):
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_run_subagent_in_thread)
                    result_content = future.result(timeout=actual_timeout + 10).strip()

                    elapsed_time = time.time() - start_time
                    logger.info(f"[spawn_subagent] SUCCESS - call_id={call_id}, elapsed={elapsed_time:.2f}s, output_len={len(result_content)}, attempts={attempt + 1}")

                    MAX_OUTPUT_LENGTH = 2000
                    if summarize_output and len(result_content) > MAX_OUTPUT_LENGTH:
                        logger.info(f"[spawn_subagent] Output exceeds {MAX_OUTPUT_LENGTH} chars, generating summary...")

                        summary = self._summarize_subagent_output(
                            task=task,
                            output=result_content,
                        )

                        log_path = None
                        try:
                            log_dir = "workspace/subagent_logs"
                            log_filename = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{call_id}.md"
                            log_path = f"{log_dir}/{log_filename}"

                            log_content = f"""# Sub-agent 执行日志

**执行时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Call ID**: {call_id}
**模型**: {model}
**角色**: default
**耗时**: {elapsed_time:.1f}s
**输出长度**: {len(result_content)} 字符

## 原始任务

{task}

## 完整输出

{result_content}
"""
                            write_result = self.file_write(log_path, log_content)
                            if write_result.startswith("OK"):
                                logger.info(f"[spawn_subagent] Full output saved to VFS: {log_path}")
                            else:
                                logger.warning(f"[spawn_subagent] Failed to save full output: {write_result}")
                                log_path = None
                        except Exception as e:
                            logger.error(f"[spawn_subagent] Error saving full output to VFS: {e}")
                            log_path = None

                        formatted_summary = self._format_summary_output(summary, len(result_content), log_path)

                        if include_stats:
                            stats_prefix = f"📊 执行统计: 耗时 {elapsed_time:.1f}s | 模型 {model} | 输出 {len(result_content)} 字符\n"
                            if model_recommendation_reason:
                                stats_prefix += f"💡 模型推荐: {model_recommendation_reason}\n"
                            stats_prefix += "\n"
                            return stats_prefix + formatted_summary
                        return formatted_summary

                    if include_stats:
                        stats_prefix = f"📊 执行统计: 耗时 {elapsed_time:.1f}s | 模型 {model} | 输出 {len(result_content)} 字符\n"
                        if model_recommendation_reason:
                            stats_prefix += f"💡 模型推荐: {model_recommendation_reason}\n"
                        stats_prefix += "\n"
                        return stats_prefix + result_content
                    return result_content
            except Exception as e:
                last_error = e
                error_type = type(e).__name__

                should_retry = (
                    max_retries > 0 and
                    attempt < max_retries and
                    (error_type in ['TimeoutError', 'timeout', 'APITimeoutError',
                                   'ConnectionError', 'ConnectionTimeout',
                                   'ReadTimeout', 'ConnectTimeout'])
                )

                if should_retry:
                    logger.warning(f"[SPAWN_SUBAGENT] Attempt {attempt + 1}/{max_retries + 1} failed: {error_type}: {e}")
                    logger.warning(f"[SPAWN_SUBAGENT] Retrying...")
                    time.sleep(2 ** attempt)
                else:
                    break

        elapsed_time = time.time() - start_time
        error_msg = f"Error: spawn_subagent failed after {attempt + 1} attempt(s): {type(last_error).__name__}: {last_error}"

        logger.error(f"[spawn_subagent] FAILED - call_id={call_id}, elapsed={elapsed_time:.2f}s, attempts={attempt + 1}, error={type(last_error).__name__}")

        error_type = type(last_error).__name__
        if 'Timeout' in error_type or 'timeout' in str(last_error):
            error_msg += f"\n\n提示: 任务执行超时（{actual_timeout}秒）。建议："
            error_msg += "\n1. 增加 timeout_seconds 参数（如 900 秒）"
            error_msg += "\n2. 使用更快的模型（如 doubao-seed-2-0-lite）"
            error_msg += "\n3. 简化任务描述"
        elif 'Connection' in error_type or 'Network' in error_type:
            error_msg += "\n\n提示: 网络连接失败。建议："
            error_msg += "\n1. 检查网络连接"
            error_msg += "\n2. 增加 max_retries 参数（如 2）"

        logger.error(f"[SPAWN_SUBAGENT] Exception: {error_msg}", exc_info=True)
        return error_msg

    @tool(description="读取子Agent的完整执行日志", category="agent")
    def read_subagent_log(self, log_path: str) -> str:
        """
        读取子代理的完整执行日志

        当 spawn_subagent 使用 summarize_output=True 时，完整输出会保存到 VFS。
        此方法用于读取那些完整日志。

        Args:
            log_path: VFS 日志文件路径，如 "workspace/subagent_logs/2024-01-15_10-30-00_xxx.md"

        Returns:
            完整的日志内容
        """
        if not log_path.startswith("workspace/subagent_logs/"):
            return "Error: 无效的日志路径，应为 workspace/subagent_logs/... 格式"

        if not log_path.endswith(".md"):
            return "Error: 日志文件应为 .md 格式"

        try:
            content = self.file_read(log_path)
            return content
        except Exception as e:
            return f"Error: 读取日志失败: {e}"
