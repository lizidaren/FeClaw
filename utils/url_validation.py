"""
统一出站 URL 校验（Q19/C8：SSRF 防护）

审计发现全仓库没有任何出站 URL 校验层（`image_url`、`web_fetch` 等直接
`httpx.get(url)`），可被利用访问云元数据（169.254.169.254）、回环/私网/内网服务。

本模块提供唯一入口 `validate_public_http_url()`：
- 只允许 http/https；
- 拒绝云元数据域名（AWS / GCP / 腾讯云 / Aliyun / 本地 metadata 等）；
- 把 hostname 解析成 IP 后，拒绝 loopback / link-local / RFC1918 / ULA /
  reserved / multicast / unspecified；
- 拒绝十进制/八进制/十六进制 IP 记法（`2130706433`、`0x7f000001` 等）。

调用方负责在每次出站请求前调用；本函数只做**初始 URL**校验（httpx 默认不跟随
重定向，follow_redirects=False 即不会把内网地址藏在重定向后）。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# 常见云元数据域名（AWS/GCP/Azure/腾讯云/阿里云/华为云等）—— 精确匹配
_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.tencentyun.com",
    "100.100.100.200",
}

# 常见的 metadata 后缀
_METADATA_SUFFIXES = (
    ".metadata.google.internal",
    ".metadata.tencentyun.com",
)


def _is_blocked_hostname(host: str) -> bool:
    """hostname 层面的元数据/内网域名阻断。"""
    host = (host or "").lower().rstrip(".")
    if not host:
        return True
    if host in _METADATA_HOSTS:
        return True
    if any(host.endswith(s) for s in _METADATA_SUFFIXES):
        return True
    return False


def _resolve_ips(host: str):
    """把一个 host（可能是 IP 字面量或域名）解析为 ipaddress 对象集合。

    覆盖 SSRF 常见绕过：十进制整数 IP、十六进制（0x7f000001）、八进制（0177...）、
    以及点分十进制的变体。
    """
    host = (host or "").strip()
    if not host:
        return []

    # 1. 直接当 IP 字面量
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    # 2. 纯数字 → 十进制整数 IP（如 2130706433 = 127.0.0.1）
    if host.isdigit():
        try:
            return [ipaddress.ip_address(int(host))]
        except ValueError:
            return []

    # 3. 十六进制 / 八进制前缀记法
    lowered = host.lower()
    if lowered.startswith("0x"):
        try:
            return [ipaddress.ip_address(int(lowered, 16))]
        except ValueError:
            return []
    if lowered.startswith("0") and len(lowered) > 1 and lowered.isdigit():
        # 八进制（017700000001）—— 注意 "0" 本身已由 isdigit 分支覆盖
        try:
            return [ipaddress.ip_address(int(lowered, 8))]
        except ValueError:
            return []

    # 4. 域名 → DNS 解析
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    ips = set()
    for info in infos:
        addr = info[4][0]
        try:
            ips.add(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return list(ips)


def validate_public_http_url(url: str) -> bool:
    """校验一个 URL 是否可安全出站请求。安全返回 True，否则 False。"""
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.hostname
    if not host:
        return False

    if _is_blocked_hostname(host):
        return False

    for ip in _resolve_ips(host):
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False

    return True
