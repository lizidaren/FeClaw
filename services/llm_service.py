"""
LLM服务封装
支持智谱AI等大模型提供商
"""

import json
import logging
import httpx
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Callable, Optional, Dict, Any, List
from config import settings
from models.database import LLMStat, SessionLocal

import asyncio

logger = logging.getLogger(__name__)


async def _parse_sse_events(response):
    """字节级 SSE 解析器，SSE 事件到达时立即 yield data 字段内容。

    相比 aiter_lines() 的行级缓冲，字节级解析能在每个事件到达时立即处理，
    避免多个事件合并输出，实现真正的逐 token 流式效果。
    """
    buffer = b""
    async for chunk in response.aiter_bytes():
        buffer += chunk
        while True:
            double_newline = buffer.find(b"\n\n")
            if double_newline == -1:
                break
            event_bytes = buffer[:double_newline]
            buffer = buffer[double_newline + 2:]
            event_text = event_bytes.decode("utf-8", errors="replace")
            for line in event_text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("data: "):
                    yield stripped[6:]


def _extract_balanced_json(text: str) -> Optional[str]:
    """
    从文本中提取 JSON 对象（使用平衡花括号算法）。
    忽略 LaTeX 公式中的嵌套花括号。
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    i = start
    in_latex = False
    latex_commands = {
        "frac", "sqrt", "binom", "left", "right", "mathbf", "mathit", "mathrm",
        "text", "mathbb", "hat", "vec", "dot", "bar", "underline", "overline",
        "overbrace", "underbrace", "overset", "underset", "substack", "begin", "end",
        "sum", "int", "lim", "min", "max", "sin", "cos", "tan", "log", "ln"
    }

    while i < len(text):
        ch = text[i]

        # 检测 LaTeX 命令
        if ch == "\\" and i + 1 < len(text):
            rest = text[i+1:].lstrip()
            cmd_match = __import__("re").match(r"([a-zA-Z]+)", rest)
            if cmd_match:
                cmd = cmd_match.group(1)
                if cmd in latex_commands:
                    in_latex = True
                    i += 1 + cmd_match.end()
                    continue

        if in_latex:
            if ch == "}":
                # 可能是 LaTeX 参数闭合
                if i + 1 < len(text) and text[i+1] in ("{", " ", ",", ")", "$", "\n"):
                    in_latex = False
            if ch == " " or ch == "\n":
                in_latex = False
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]
        i += 1
    return None


class LLMProvider(ABC):
    """LLM提供商抽象基类"""

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False
    ) -> AsyncGenerator[str, None]:
        pass


class DeepSeekProvider(LLMProvider):
    """DeepSeek提供商"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.deepseek.com"

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False
    ) -> AsyncGenerator[str, None]:
        """调用DeepSeek API"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model or settings.AGENT_LLM_MODEL,
            "messages": messages,
            "stream": stream,
        }

        if response_format:
            payload["response_format"] = response_format

        if max_tokens:
            payload["max_tokens"] = max_tokens

        # DeepSeek 的深度思考参数
        if reasoning_effort and not disable_thinking:
            payload["thinking"] = {"type": "enabled"}
        elif disable_thinking:
            payload["thinking"] = {"type": "disabled"}

        async with httpx.AsyncClient(timeout=300.0) as client:
            if stream:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                parsed = json.loads(data)
                                content = parsed.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
            else:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                result = response.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                yield content


class DoubaoProvider(LLMProvider):
    """豆包提供商"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://ark.cn-beijing.volces.com/api/v3"

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False,
    ) -> AsyncGenerator[str, None]:
        """调用豆包API"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model or "doubao-seed-2-0-lite-260215",
            "messages": messages,
            "stream": stream,
        }

        if response_format:
            payload["response_format"] = response_format

        if max_tokens:
            payload["max_tokens"] = max_tokens

        # 豆包的深度思考参数 reasoning_effort
        if reasoning_effort and not disable_thinking:
            payload["reasoning_effort"] = reasoning_effort

        async with httpx.AsyncClient(timeout=300.0) as client:
            if stream:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                parsed = json.loads(data)
                                # 豆包的推理过程在 reasoning_content 字段中
                                reasoning_content = parsed.get("choices", [{}])[0].get("delta", {}).get("reasoning_content", "")
                                content = parsed.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                # 优先输出推理过程，然后再输出内容
                                if reasoning_content:
                                    yield reasoning_content
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
            else:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                result = response.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                yield content


class ZhipuAIProvider(LLMProvider):
    """智谱AI提供商"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://open.bigmodel.cn/api/paas/v4"

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False
    ) -> AsyncGenerator[str, None]:
        """调用智谱AI API"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model or settings.DEFAULT_LLM_MODEL,
            "messages": messages,
            "stream": stream,
        }

        if max_tokens:
            payload["max_tokens"] = max_tokens

        if response_format:
            payload["response_format"] = response_format

        # 智谱AI的深度思考参数
        if disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        elif reasoning_effort:
            # 智谱AI使用 thinking 参数，格式为 {"type": "enabled"}
            payload["thinking"] = {"type": "enabled"}

        async with httpx.AsyncClient(timeout=300.0) as client:
            if stream:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                parsed = json.loads(data)
                                content = parsed.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
            else:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                result = response.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                yield content


class KimiProvider(LLMProvider):
    """Kimi (Moonshot) AI 大模型提供商"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = settings.KIMI_BASE_URL

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False
    ) -> AsyncGenerator[str, None]:
        """调用 Kimi API (OpenAI 兼容接口)"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model or settings.KIMI_MODEL,
            "messages": messages,
            "stream": stream,
        }

        if max_tokens:
            payload["max_tokens"] = max_tokens

        if response_format:
            payload["response_format"] = response_format

        async with httpx.AsyncClient(timeout=300.0) as client:
            if stream:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                parsed = json.loads(data)
                                content = parsed.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
            else:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                result = response.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                yield content


class QwenProvider(LLMProvider):
    """Qwen (通义千问) 大模型提供商"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False
    ) -> AsyncGenerator[str, None]:
        """调用 Qwen API (OpenAI 兼容接口)"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model or "qwen3.6-flash",
            "messages": messages,
            "stream": stream,
        }

        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format
        if disable_thinking:
            payload["thinking"] = {"type": "disabled"}

        _debug_messages = [
            {"role": m["role"], "content": m["content"][:200] + "..." if len(m["content"]) > 200 else m["content"]}
            for m in messages
        ]
        logger.info(f"[LLM_REQUEST] provider=qwen model={payload['model']} messages={_debug_messages}")

        async with httpx.AsyncClient(timeout=300.0) as client:
            if stream:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                parsed = json.loads(data)
                                choices = parsed.get("choices", [])
                                if not choices:
                                    continue
                                content = choices[0].get("delta", {}).get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
            else:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                result = response.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                yield content


# Provider 配置映射（共享常量，避免在多处重复）
PROVIDER_CONFIG = {
    "deepseek": {
        "api_key_attr": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com"
    },
    "zhipuai": {
        "api_key_attr": "ZHIPU_API_KEY",
        "base_url": "https://open.bigmodel.cn/api/paas/v4"
    },
    "doubao": {
        "api_key_attr": "DOUBAO_API_KEY",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3"
    },
    "kimi": {
        "api_key_attr": "KIMI_API_KEY",
        "base_url": None  # 使用 settings.KIMI_BASE_URL
    },
    "qwen": {
        "api_key_attr": "QWEN_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    },
}


def _resolve_provider(provider_name: str) -> tuple:
    """
    共享的 provider 解析函数，用于 chat_with_tools 和 chat_with_tools_stream。

    Returns:
        (api_key, base_url) 或 raises ValueError
    """
    config = PROVIDER_CONFIG.get(provider_name)
    if not config:
        raise ValueError(f"Unknown provider for tools: {provider_name}")

    api_key = getattr(settings, config["api_key_attr"], None)
    base_url = config["base_url"]
    if base_url is None:
        base_url = settings.KIMI_BASE_URL

    if not api_key:
        raise ValueError(f"API key not configured for provider: {provider_name}")

    return api_key, base_url


class LLMService:
    """LLM服务管理器"""

    def __init__(self):
        self.providers: Dict[str, LLMProvider] = {}
        # 共享 httpx 客户端（连接池复用，避免每次请求新建 TCP 连接）
        self._http_client: Optional[httpx.AsyncClient] = None

        # 初始化提供商
        if settings.ZHIPU_API_KEY:
            self.providers["zhipuai"] = ZhipuAIProvider(settings.ZHIPU_API_KEY)
        if settings.DEEPSEEK_API_KEY:
            self.providers["deepseek"] = DeepSeekProvider(settings.DEEPSEEK_API_KEY)
        if settings.DOUBAO_API_KEY:
            self.providers["doubao"] = DoubaoProvider(settings.DOUBAO_API_KEY)
        if settings.KIMI_API_KEY:
            self.providers["kimi"] = KimiProvider(settings.KIMI_API_KEY)
        if settings.QWEN_API_KEY:
            self.providers["qwen"] = QwenProvider(settings.QWEN_API_KEY)

    async def _ensure_http_client(self) -> httpx.AsyncClient:
        """确保共享 httpx 客户端已初始化（懒加载，连接池复用）"""
        if self._http_client is None or self._http_client.is_closed:
            limits = httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30.0
            )
            timeout = httpx.Timeout(
                connect=10.0,
                read=300.0,
                write=30.0,
                pool=10.0
            )
            self._http_client = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                http2=True
            )
        return self._http_client

    async def close_http_client(self):
        """关闭共享 HTTP 客户端（服务关闭时调用）"""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def _retry_call(self, coro_factory, max_retries: int = 3, base_delay: float = 1.0):
        """指数退避重试 LLM API 调用

        Args:
            coro_factory: 返回 awaitable 的工厂函数，每次调用创建新的请求
            max_retries: 最大重试次数
            base_delay: 基础延迟秒数，指数增长
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                return await coro_factory()
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(f"[LLM] Timeout (attempt {attempt + 1}/{max_retries}): {e}")
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (429, 502, 503, 504):
                    last_error = e
                    logger.warning(f"[LLM] HTTP {status} (attempt {attempt + 1}/{max_retries})")
                else:
                    raise  # 非暂时性错误（401/402/403/5xx等），不重试
            except Exception as e:
                # 网络错误（连接重置等）重试
                last_error = e
                logger.warning(f"[LLM] Network error (attempt {attempt + 1}/{max_retries}): {e}")

            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
        raise last_error

    def get_provider(self, provider_name: str) -> LLMProvider:
        """获取提供商"""
        provider = self.providers.get(provider_name)
        if not provider:
            raise ValueError(f"Unknown LLM provider: {provider_name}")
        return provider
    
    async def chat(
        self,
        messages: List[Dict[str, str]],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False,
        request_type: str = "chat"
    ) -> AsyncGenerator[str, None]:
        """调用LLM聊天"""

        provider_name = provider or settings.DEFAULT_LLM_PROVIDER
        provider_instance = self.get_provider(provider_name)

        # 记录统计（异步，不阻塞主流程）
        asyncio.create_task(self._record_stat(provider_name, model or settings.DEFAULT_LLM_MODEL, request_type))

        async for chunk in provider_instance.chat(
            messages=messages,
            stream=stream,
            response_format=response_format,
            model=model,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            disable_thinking=disable_thinking
        ):
            yield chunk
    
    async def chat_json(
        self,
        messages: List[Dict[str, str]],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        disable_thinking: bool = False,
        request_type: str = "chat"
    ) -> Dict[str, Any]:
        """调用LLM并解析JSON响应"""
        
        full_response = ""
        async for chunk in self.chat(
            messages=messages,
            provider=provider,
            model=model,
            stream=False,
            response_format={"type": "json_object"},
            disable_thinking=disable_thinking,
            request_type=request_type
        ):
            full_response += chunk

        # 清理 markdown 代码块标记
        import re
        cleaned = re.sub(r"^```(?:json)?\s*", "", full_response, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)

        try:
            return json.loads(cleaned.strip())
        except json.JSONDecodeError:
            # 尝试用平衡花括号算法提取 JSON
            json_candidate = _extract_balanced_json(cleaned)
            if json_candidate:
                return json.loads(json_candidate)
            # 最后尝试：直接strip后解析
            cleaned = full_response.strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                raise ValueError(f"LLM 返回了无效的 JSON 响应: {cleaned[:200]}")
    
    async def chat_with_tools(
        self,
        messages: List[Dict[str, str]],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        request_type: str = "agent_tool",
        tool_filter: Optional[Callable[[str, dict], bool]] = None,
        max_filter_retries: int = 3,
    ) -> Dict[str, Any]:
        """调用LLM支持工具调用（function calling），返回包含tool_calls的响应（非流式）

        Args:
            tool_filter: 可选回调 (func_name, args) -> bool，返回 False 时拒绝该工具调用，
                         让 LLM 看到错误后修正（重试最多 max_filter_retries 轮）
            max_filter_retries: tool_filter 拒绝后的最大重试轮数
        """

        provider_name = provider or settings.AGENT_LLM_PROVIDER
        api_key, base_url = _resolve_provider(provider_name)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model or settings.AGENT_LLM_MODEL,
            "messages": messages,
            "stream": False,
        }

        if tools:
            payload["tools"] = tools

        # 记录统计
        asyncio.create_task(self._record_stat(provider_name, model or settings.AGENT_LLM_MODEL, request_type))

        client = await self._ensure_http_client()

        async def _call():
            response = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            return response.json()

        # 尝试主提供商，失败时 fallback 到备用提供商
        try:
            result = await self._retry_call(_call)
        except Exception as primary_err:
            fallback_prov = getattr(settings, "FALLBACK_LLM_PROVIDER", "")
            if not fallback_prov or fallback_prov == provider_name:
                raise primary_err

            fallback_model = getattr(settings, "FALLBACK_LLM_MODEL", "glm-4.7")
            logger.warning(
                "LLM fallback: %s/%s failed (%s), switching to %s/%s",
                provider_name, payload.get("model", "?"), primary_err,
                fallback_prov, fallback_model
            )
            provider_name = fallback_prov
            payload["model"] = fallback_model
            api_key, base_url = _resolve_provider(provider_name)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            result = await self._retry_call(_call)

        message = result.get("choices", [{}])[0].get("message", {})
        content = message.get("content", "") or ""
        tool_calls = message.get("tool_calls", None)

        # ── tool_filter 拒绝重试循环 ──────────────────────
        if tool_filter and tool_calls:
            for _ in range(max_filter_retries):
                # 检查所有 tool_calls 是否通过 filter
                all_allowed = True
                for tc in tool_calls:
                    func_name = tc.get("function", {}).get("name", "")
                    try:
                        args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    if not tool_filter(func_name, args):
                        all_allowed = False
                        break

                if all_allowed:
                    break  # 全部通过，无需重试

                # 把拒绝结果告诉 LLM，让它修正
                messages.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    func_name = tc.get("function", {}).get("name", "")
                    try:
                        args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    if not tool_filter(func_name, args):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": (
                                f"Error: 不允许调用 {func_name}。"
                                f"请使用允许的路径（/workspace/agent/ 下的文件）。"
                            ),
                        })
                    else:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": "(允许，待执行)",
                        })

                # 让 LLM 重新生成
                result = await self._retry_call(_call)
                message = result.get("choices", [{}])[0].get("message", {})
                content = message.get("content", "") or ""
                tool_calls = message.get("tool_calls", None)
                if not tool_calls:
                    break
        # ── 拒绝重试循环结束 ──────────────────────────────

        return {
            "content": content,
            "tool_calls": tool_calls
        }
    
    async def chat_with_tools_stream(
        self,
        messages: List[Dict[str, str]],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        request_type: str = "agent_tool_stream",
        reasoning_effort: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式调用LLM支持工具调用（使用共享 httpx 客户端，连接池复用）

        Yields:
            {"type": "token", "content": "..."} - 文本片段
            {"type": "done", "content": "...", "tool_calls": [...]} - 流结束，包含完整信息
        """

        provider_name = provider or settings.AGENT_LLM_PROVIDER
        api_key, base_url = _resolve_provider(provider_name)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model or settings.AGENT_LLM_MODEL,
            "messages": messages,
            "stream": True,  # 流式模式
        }

        if tools:
            payload["tools"] = tools

        # DeepSeek 深度思考控制
        if provider_name == "deepseek":
            if reasoning_effort in ("high", "max"):
                payload["thinking"] = {"type": "enabled"}
            elif reasoning_effort == "off":
                payload["thinking"] = {"type": "disabled"}

        # 记录统计
        asyncio.create_task(self._record_stat(provider_name, model or settings.AGENT_LLM_MODEL, request_type))

        full_content = ""
        full_reasoning = ""
        tool_calls_data = None

        client = await self._ensure_http_client()

        async def _connect():
            """建立流式连接（可重试）"""
            return await client.send(
                client.build_request(
                    "POST",
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ),
                stream=True
            )

        # 尝试主提供商，失败时 fallback 到备用提供商
        try:
            response = await self._retry_call(_connect)
        except Exception as primary_err:
            fallback_prov = getattr(settings, "FALLBACK_LLM_PROVIDER", "")
            if not fallback_prov or fallback_prov == provider_name:
                raise primary_err

            fallback_model = getattr(settings, "FALLBACK_LLM_MODEL", "glm-4.7")
            logger.warning(
                "LLM fallback: %s/%s failed (%s), switching to %s/%s",
                provider_name, payload.get("model", "?"), primary_err,
                fallback_prov, fallback_model
            )
            provider_name = fallback_prov
            payload["model"] = fallback_model
            payload.pop("thinking", None)  # DeepSeek 特定参数，fallback 时移除
            api_key, base_url = _resolve_provider(provider_name)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            response = await self._retry_call(_connect)

        try:
            response.raise_for_status()

            async for data in _parse_sse_events(response):
                if data == "[DONE]":
                    break

                try:
                    parsed = json.loads(data)
                    choices = parsed.get("choices", [])
                    if not choices:
                        continue  # 某些模型的中间 chunk choices 为空，跳过
                    choice = choices[0]

                    # 处理增量内容
                    delta = choice.get("delta", {})
                    content_chunk = delta.get("content", "")

                    if content_chunk:
                        # 过滤 XML 工具调用标签和 "🔧 执行工具:" 文本
                        import re as _ft
                        if _ft.search(r'</?invoke[^>]*>|</?parameter[^>]*>|🔧\s*执行工具:', content_chunk):
                            full_content += content_chunk  # 累积到 full_content 但不发送给用户
                        else:
                            full_content += content_chunk
                            yield {"type": "token", "content": content_chunk}

                    # 深度思考过程（deepseek 必须在下一轮传回 reasoning_content）
                    reasoning_chunk = delta.get("reasoning_content", "")
                    if reasoning_chunk:
                        full_reasoning += reasoning_chunk
                        # 不 yield 给用户——思考链不应出现在对话中

                    # 处理 tool_calls（在流的最后部分）
                    delta_tool_calls = delta.get("tool_calls", None)
                    if delta_tool_calls:
                        # 累积 tool_calls 信息
                        if tool_calls_data is None:
                            tool_calls_data = []

                        for tc in delta_tool_calls:
                            idx = tc.get("index", 0)
                            # 找到或创建对应的 tool_call
                            while len(tool_calls_data) <= idx:
                                tool_calls_data.append({
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""}
                                })

                            # 更新信息
                            if tc.get("id"):
                                tool_calls_data[idx]["id"] = tc["id"]
                            if tc.get("function", {}).get("name"):
                                tool_calls_data[idx]["function"]["name"] = tc["function"]["name"]
                            if tc.get("function", {}).get("arguments"):
                                tool_calls_data[idx]["function"]["arguments"] += tc["function"]["arguments"]

                except json.JSONDecodeError:
                    continue
        finally:
            await response.aclose()
        
        # 清理 full_content 中残留的 XML 工具调用标签
        import re as _re
        full_content = _re.sub(r'</?invoke[^>]*>|</?parameter[^>]*>', '', full_content).strip()
        
        # 如果模型在文本中写了 "🔧 执行工具: xxx" 但实际未触发 tool_call，自动识别并创建工具调用
        if tool_calls_data is None:
            _fake_tool_pattern = _re.compile(r'🔧\s*执行工具:\s*([\w_]+)')
            _fake_match = _fake_tool_pattern.search(full_content)
            if _fake_match:
                _tool_name = _fake_match.group(1)
                # 如果是已知工具，创建 tool_call
                from services.tool_registry import get_tool
                if get_tool(_tool_name) is not None:
                    tool_calls_data = [{
                        "id": f"call_{int(time.monotonic()*1000)}",
                        "type": "function",
                        "function": {
                            "name": _tool_name,
                            "arguments": "{}"
                        }
                    }]
                # 从文本中去掉这一行
                full_content = _fake_tool_pattern.sub('', full_content).strip()
        
        # 从文本中去掉 "🔧 执行工具: xxx" 模式（防止模型模仿格式输出）
        _tool_pattern_all = _re.compile(r'🔧\s*执行工具:\s*[\w_]+')
        full_content = _tool_pattern_all.sub('', full_content).strip()
        
        # 流结束，返回完整信息
        # 如果 DeepSeek 使用了 XML 格式的工具调用 (<invoke name="xxx">),
        # 但 JSON 解析未识别到 tool_calls，则从文本中提取
        if tool_calls_data is None:
            _invoke_pattern = _re.compile(r'<invoke\s+name="([^"]+)"\s*>')
            _match = _invoke_pattern.search(full_content)
            if _match:
                _tool_name = _match.group(1)
                # 提取参数（如果有）
                _params = {}
                _param_pattern = _re.compile(r'<parameter\s+name="([^"]+)">([^<]*)</parameter>')
                for _pm in _param_pattern.finditer(full_content):
                    _params[_pm.group(1)] = _pm.group(2)
                # 构建标准 tool_call 格式
                if not _params:
                    _params = {"query": full_content.split('>')[0].split('"')[1] if '"' in full_content.split('>')[0] else ""}
                tool_calls_data = [{
                    "id": f"call_{int(time.monotonic()*1000)}",
                    "type": "function",
                    "function": {
                        "name": _tool_name,
                        "arguments": json.dumps(_params, ensure_ascii=False)
                    }
                }]
        yield {
            "type": "done",
            "content": full_content,
            "reasoning_content": full_reasoning,
            "tool_calls": tool_calls_data
        }
    
    @staticmethod
    def estimate_tokens(text) -> int:
        """预估 token 数量（中英文混合场景）。

        中文每个字符约 1.5 tokens，英文约 4 chars/token。
        接受 str 或 List[Dict]，后者会先序列化为 JSON 再估算。
        """
        from services.message_compactor import estimate_tokens as _est
        return _est(text)
    
    async def _record_stat(self, provider: str, model: str, request_type: str):
        """记录LLM调用统计"""
        db = SessionLocal()
        try:
            stat = LLMStat(
                provider=provider,
                model=model,
                request_type=request_type
            )
            db.add(stat)
            db.commit()
        except Exception as e:
            logger.error(f"Failed to record LLM stat: {e}")
        finally:
            db.close()


# 全局LLM服务实例
llm_service = LLMService()