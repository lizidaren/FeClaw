"""
WSL2 DNS 抢修：劫持 socket.getaddrinfo，3 秒超时 + DNS 挂死走缓存。

用法：
    install_global_fallback()  # 启动时调用一次

核心策略（WSL 下）：
  1. 每次 DNS 解析设 3s 超时（独立线程）
  2. 超时 DNS 挂死 → 走缓存 IP
  3. 无缓存 → 抛出 OSError 让 httpx/业务代码自行处理
  4. 非 WSL → 完全透传
"""

import logging
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_dns_cache: Dict[str, List[Tuple]] = {}
_lock = threading.Lock()
_original_getaddrinfo = socket.getaddrinfo
_is_wsl = "microsoft" in __import__("platform").uname().release.lower()

# WSL 线程池（最多 4 个并发 DNS 查询）
_dns_pool: Optional[ThreadPoolExecutor] = None
_DNS_TIMEOUT = 3.0

# WSL 下已知不可靠的域名 → 硬编码 IP fallback
_HARDCODED_FALLBACKS: Dict[str, List[Tuple]] = {
    "firstentrance-gz01-1257148458.cos.ap-guangzhou.myqcloud.com": [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('175.6.91.141', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('27.155.119.140', 443)),
    ],
    "cos.ap-guangzhou.myqcloud.com": [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('175.6.91.141', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('27.155.119.140', 443)),
    ],
    "sts.tencentcloudapi.com": [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('81.71.197.35', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('106.53.137.165', 443)),
    ],
}


def pre_resolve(*hostnames: str) -> None:
    """在启动时预热解析域名并缓存（非 WSL 环境执行，WSL 也用线程池尝试 3s）"""
    max_wait = 0.0
    for hostname in hostnames:
        try:
            if _is_wsl:
                # WSL：带超时的尝试（不阻塞启动）
                addrs = _resolve_or_timeout(hostname, 443, 0, 0, 0, 0)
                max_wait = max(max_wait, _DNS_TIMEOUT)
            else:
                addrs = _original_getaddrinfo(hostname, 443)
            with _lock:
                _dns_cache[hostname] = addrs
            logger.info(f"✅ [DNS] Pre-resolved {hostname} -> {len(addrs)} IPs")
        except Exception as e:
            logger.debug(f"[DNS] Pre-resolve {hostname} not available: {e}")
    if max_wait > 0:
        logger.info(f"[DNS] WSL pre-resolve took ~{max_wait:.0f}s (timed-out domains will retry at runtime)")


def _resolve_or_timeout(host: str, port: int, family: int, type_: int, proto: int, flags: int) -> List[Tuple]:
    """带超时的 DNS 解析，超时抛出 TimeoutError"""
    global _dns_pool
    if _dns_pool is None:
        _dns_pool = ThreadPoolExecutor(max_workers=4)
    fut = _dns_pool.submit(_original_getaddrinfo, host, port, family, type_, proto, flags)
    return fut.result(timeout=_DNS_TIMEOUT)


def install_global_fallback() -> None:
    """安装全局 socket.getaddrinfo 补丁（仅 WSL 生效）"""
    if not _is_wsl:
        return

    def _patched(host, port, family=0, type_=0, proto=0, flags=0):
        if host is None:
            return _original_getaddrinfo(host, port, family, type_, proto, flags)
        host_str = host.decode() if isinstance(host, bytes) else host

        # localhost、127.0.0.1 等不走超时机制，直接透传
        if host_str in ('localhost', '127.0.0.1', '::1'):
            return _original_getaddrinfo(host, port, family, type_, proto, flags)

        # 1) 带超时的 DNS 解析
        try:
            return _resolve_or_timeout(host, port, family, type_, proto, flags)
        except TimeoutError:
            cached = get_cached(host_str)
            if cached:
                logger.debug(f"[DNS] ⏱ timeout, using cached IPs for {host_str}")
                return cached
            logger.warning(f"[DNS] ⏱ timeout + no cache for {host_str}")
        except Exception as e:
            cached = get_cached(host_str)
            if cached:
                logger.debug(f"[DNS] ⚠️ {e}, using cached IPs for {host_str}")
                return cached
            raise

        # 2) 硬编码 fallback（WSL 常用域名）
        hardcoded = _HARDCODED_FALLBACKS.get(host_str)
        if hardcoded:
            logger.warning(f"[DNS] Using hardcoded IPs for {host_str}")
            return hardcoded

        # 3) 无缓存 → 再试一次
        try:
            return _resolve_or_timeout(host, port, family, type_, proto, flags)
        except TimeoutError:
            pass

        raise OSError(f"Temporary failure in name resolution: {host_str}")

    socket.getaddrinfo = _patched
    logger.info("✅ [DNS] Global fallback installed (WSL, 3s timeout + cache)")


def get_cached(hostname: str) -> Optional[List[Tuple]]:
    with _lock:
        return _dns_cache.get(hostname)
