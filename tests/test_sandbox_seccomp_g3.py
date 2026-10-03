"""
FIX-F G3 —— 沙箱 seccomp 收敛 socket 家族 + netns 屏蔽 169.254.0.0/16

审计（AUDIT-R1 §G3）：
  1. `services/sandbox/base.py` seccomp 白名单显式放行 socket/connect/sendto/recvfrom
     等全部 socket 系统调用 —— 沙箱内可创建 AF_INET 套接字（残余云元数据面）。
  2. `scripts/init_sandbox_netns.sh` PRIVATE_NETS 缺 `169.254.0.0/16`
     （AWS 169.254.169.254 / 腾讯 169.254.0.23 云元数据）。

修复：socket(2) 只放行 AF_UNIX（VFS 回环 API 用），AF_INET/AF_INET6 一律 EPERM；
netns 的 PRIVATE_NETS 补 169.254.0.0/16。

本测试用一个小型 seccomp BPF 解释器，对生成的字节码做「(syscall, arg0) → 动作」
矩阵断言，验证 socket() 的 domain 门禁真生效、其余白名单不受影响。
"""
import os
import re
import struct

import pytest

from services.sandbox.base import (
    _create_seccomp_bpf,
    SECCOMP_RET_ALLOW,
    SECCOMP_RET_ERRNO,
    EPERM,
    SYS_SOCKET,
    SYS_READ,
    AF_UNIX,
    AF_INET,
    AF_INET6,
)

AUDIT_ARCH_X86_64 = 0xC000003E

# 已知不在白名单的「非法」syscall（用于验证默认拒绝仍生效）
SYS_UNKNOWN = 9999


def _decode(bpf: bytes):
    insns = []
    for i in range(0, len(bpf), 8):
        code, jt, jf, k = struct.unpack("<HBBI", bpf[i:i + 8])
        insns.append((code, jt, jf, k))
    return insns


def _run(bpf: bytes, nr: int, arg0: int):
    """解释 seccomp BPF，返回 RET 动作值。seccomp_data 布局（x86_64）：
    nr@0, arch@4, ip@8, args[0..5]@16..。这里只填充 nr / arch / arg0。"""
    insns = _decode(bpf)
    data = bytearray(64)
    struct.pack_into("<I", data, 0, nr)
    struct.pack_into("<I", data, 4, AUDIT_ARCH_X86_64)
    struct.pack_into("<Q", data, 16, arg0)

    acc = 0
    pc = 0
    while 0 <= pc < len(insns):
        code, jt, jf, k = insns[pc]
        cls = code & 0x07
        if cls == 0x00:  # BPF_LD
            mode = code & 0xE0
            if mode == 0x20:  # BPF_ABS
                size = code & 0x18
                if size == 0x00:  # BPF_W (32-bit)
                    acc = struct.unpack_from("<I", data, k)[0]
                else:
                    raise NotImplementedError(f"LD size 0x{size:x}")
            else:
                raise NotImplementedError(f"LD mode 0x{mode:x}")
            pc += 1
        elif cls == 0x05:  # BPF_JMP
            jop = code & 0xF0
            if jop == 0x10:  # BPF_JEQ
                pc += (jt + 1) if acc == k else (jf + 1)
            elif jop == 0x00:  # BPF_JA
                pc += k + 1
            else:
                raise NotImplementedError(f"JMP op 0x{jop:x}")
        elif cls == 0x06:  # BPF_RET
            return k
        else:
            raise NotImplementedError(f"opcode class 0x{cls:x}")
    return None


@pytest.mark.skipif(_create_seccomp_bpf() is None, reason="non-x86_64: BPF not generated")
class TestSeccompSocketDomain:
    def _bpf(self):
        return _create_seccomp_bpf()

    def test_af_unix_socket_allowed(self):
        assert _run(self._bpf(), SYS_SOCKET, AF_UNIX) == SECCOMP_RET_ALLOW

    def test_af_inet_socket_denied(self):
        assert _run(self._bpf(), SYS_SOCKET, AF_INET) == (SECCOMP_RET_ERRNO | EPERM)

    def test_af_inet6_socket_denied(self):
        assert _run(self._bpf(), SYS_SOCKET, AF_INET6) == (SECCOMP_RET_ERRNO | EPERM)

    def test_non_socket_syscall_still_whitelisted(self):
        assert _run(self._bpf(), SYS_READ, 0) == SECCOMP_RET_ALLOW

    def test_unknown_syscall_still_denied(self):
        assert _run(self._bpf(), SYS_UNKNOWN, 0) == (SECCOMP_RET_ERRNO | EPERM)


class TestNetnsPrivateNets:
    def test_169_254_blocked(self):
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "init_sandbox_netns.sh",
        )
        with open(script) as f:
            content = f.read()
        m = re.search(r"PRIVATE_NETS=\((.*?)\)", content, re.S)
        assert m is not None, "PRIVATE_NETS not found in init_sandbox_netns.sh"
        nets = re.findall(r'"([^"]+)"', m.group(1))
        assert "169.254.0.0/16" in nets, "169.254.0.0/16 missing from PRIVATE_NETS"
