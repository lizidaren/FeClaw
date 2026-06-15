"""
Seccomp BPF 沙箱基础 + VFS Bootstrap 注入代码生成

- Seccomp BPF 常量 / 字节码生成 / 模块级单例
- VFSBootstrap: 生成 VFS monkey-patch 注入代码
- _check_fuse_cached: FUSE 可用性检查（模块级缓存）
"""
import base64
import json
import logging
import platform
import struct
import time as _time_module
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# Seccomp BPF Filter - Syscall Constants (x86_64)
# ============================================================================

# x86_64 syscall numbers
SYS_READ = 0
SYS_WRITE = 1
SYS_OPEN = 2
SYS_CLOSE = 3
SYS_STAT = 4
SYS_FSTAT = 5
SYS_LSTAT = 6
SYS_POLL = 7
SYS_LSEEK = 8
SYS_MMAP = 9
SYS_MPROTECT = 10
SYS_MUNMAP = 11
SYS_BRK = 12
SYS_RT_SIGACTION = 13
SYS_RT_SIGPROCMASK = 14
SYS_RT_SIGRETURN = 15
SYS_IOCTL = 16
SYS_PREAD64 = 17
SYS_PWRITE64 = 18
SYS_READV = 19
SYS_WRITEV = 20
SYS_ACCESS = 21
SYS_PIPE = 22
SYS_SELECT = 23
SYS_SCHED_YIELD = 24
SYS_MREMAP = 25
SYS_MSYNC = 26
SYS_MINCORE = 27
SYS_MADVISE = 28
SYS_SHMGET = 29
SYS_SHMAT = 30
SYS_SHMCTL = 31
SYS_SHMDT = 67
SYS_DUP = 32
SYS_DUP2 = 33
SYS_NANOSLEEP = 35
SYS_GETITIMER = 36
SYS_SETITIMER = 38
SYS_GETPID = 39
SYS_SENDFILE = 40
SYS_SOCKET = 41
SYS_CONNECT = 42
SYS_ACCEPT = 43
SYS_SENDTO = 44
SYS_RECVFROM = 45
SYS_SENDMSG = 46
SYS_RECVMSG = 47
SYS_SHUTDOWN = 48
SYS_BIND = 49
SYS_LISTEN = 50
SYS_GETSOCKNAME = 51
SYS_GETPEERNAME = 52
SYS_SETSOCKOPT = 54
SYS_GETSOCKOPT = 55
SYS_CLONE = 56
SYS_EXECVE = 59
SYS_EXIT = 60
SYS_WAIT4 = 61
SYS_KILL = 62
SYS_UNAME = 63
SYS_FCNTL = 72
SYS_FLOCK = 73
SYS_FSYNC = 74
SYS_FDATASYNC = 75
SYS_TRUNCATE = 76
SYS_FTRUNCATE = 77
SYS_GETDENTS = 78
SYS_GETCWD = 79
SYS_CHDIR = 80
SYS_FCHDIR = 81
SYS_RENAME = 82
SYS_MKDIR = 83
SYS_RMDIR = 84
SYS_CREAT = 85
SYS_LINK = 86
SYS_UNLINK = 87
SYS_SYMLINK = 88
SYS_READLINK = 89
SYS_CHMOD = 90
SYS_FCHMOD = 91
SYS_CHOWN = 92
SYS_FCHOWN = 93
SYS_LCHOWN = 94
SYS_UMASK = 95
SYS_GETTIMEOFDAY = 96
SYS_GETRUSAGE = 98
SYS_SYSINFO = 99
SYS_TIMES = 100
SYS_GETUID = 102
SYS_SETUID = 105
SYS_GETGID = 104
SYS_SETGID = 106
SYS_GETEUID = 107
SYS_GETEGID = 108
SYS_GETPPID = 110
SYS_GETPGID = 121
SYS_GETTID = 186
SYS_GETDENTS64 = 217
SYS_SET_TID_ADDRESS = 218
SYS_FUTEX = 202
SYS_CLOCK_GETTIME = 228
SYS_CLOCK_GETRES = 229
SYS_CLOCK_NANOSLEEP = 230
SYS_EXIT_GROUP = 231
SYS_EPOLL_WAIT = 232
SYS_EPOLL_CTL = 233
SYS_TGKILL = 234
SYS_UTIMENSAT = 235
SYS_SET_ROBUST_LIST = 273
SYS_GET_ROBUST_LIST = 274
SYS_SCHED_GETAFFINITY = 204
SYS_SCHED_SETAFFINITY = 203
SYS_SCHED_GETPARAM = 143
SYS_SCHED_GETSCHEDULER = 146
SYS_GETCPU = 309
SYS_GETRANDOM = 318
SYS_COPY_FILE_RANGE = 326
SYS_GETGROUPS = 115
SYS_SETGROUPS = 206
SYS_SIGALTSTACK = 131
SYS_MEMFD_CREATE = 319
SYS_EVENTFD2 = 290
SYS_PIPE2 = 293
SYS_DUP3 = 292
SYS_OPENAT = 257
SYS_MKDIRAT = 258
SYS_UNLINKAT = 263
SYS_RENAMEAT = 264
SYS_LINKAT = 265
SYS_SYMLINKAT = 266
SYS_READLINKAT = 267
SYS_FCHMODAT = 268
SYS_FACCESSAT = 269
SYS_PSELECT6 = 270
SYS_PPOLL = 271
SYS_NEWFSTATAT = 262
SYS_CLONE3 = 435
SYS_RSEQ = 334
SYS_PERSONALITY = 135
SYS_PRCTL = 157
SYS_ARCH_PRCTL = 158
SYS_SIGNALFD4 = 289
SYS_RT_SIGSUSPEND = 130
SYS_SETPGID = 109
SYS_STATFS = 137
SYS_TIMER_CREATE = 222
SYS_TIMER_SETTIME = 223
SYS_TIMERFD_CREATE = 283
SYS_TIMERFD_SETTIME = 286
SYS_TIMERFD_GETTIME = 287
SYS_TIMER_CREATE = 222
SYS_TIMER_SETTIME = 223
SYS_INOTIFY_INIT = 253
SYS_INOTIFY_ADD_WATCH = 254
SYS_INOTIFY_RM_WATCH = 255
SYS_ACCEPT4 = 288
SYS_RECVMMSG = 299
SYS_SENDMMSG = 307
SYS_GETSID = 124
SYS_SETSID = 112
SYS_GETPGRP = 111
SYS_PRLIMIT64 = 302
SYS_UNSHARE = 310
SYS_MOUNT = 165
SYS_UMOUNT2 = 166
SYS_PIVOT_ROOT = 155
SYS_GETRLIMIT = 97
SYS_SETRLIMIT = 160
SYS_TIME = 201
SYS_CAPGET = 125
SYS_CAPSET = 126

# seccomp return values
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_KILL = 0x00000000
SECCOMP_RET_ERRNO = 0x00050000  # returns specified errno
SECCOMP_RET_TRAP = 0x00030000

# Errno for denied syscalls
EPERM = 1

# AUDIT_ARCH_X86_64
AUDIT_ARCH_X86_64 = 0xC000003E

# BPF instruction constants
BPF_LD = 0x00
BPF_JMP = 0x05
BPF_RET = 0x06
BPF_JEQ = 0x10
BPF_W = 0x00
BPF_ABS = 0x20


def _build_bpf(code: int, jt: int, jf: int, k: int) -> bytes:
    """Build a single sock_filter instruction (8 bytes)."""
    return struct.pack("<HBBI", code, jt, jf, k)


def _create_seccomp_bpf() -> Optional[bytes]:
    """
    生成 seccomp BPF 字节码（白名单模式）

    只允许 Python 运行 + Unix Domain Socket (VFS通信) 所需的 syscall。
    非白名单 syscall 返回 EPERM（不是 KILL，避免进程崩溃）。

    BPF 布局:
      [0] LD arch@[4]
      [1] JEQ AUDIT_ARCH_X86_64 → +1 (go to [3]), else [2] KILL
      [2] RET KILL          (非 x86_64 直接杀)
      [3] LD syscall_nr@[0]
      [4..N+3] JEQ syscall_k → +(N-k+1) (to ALLOW), else +0 (next check)
      [N+4] RET ERRNO(EPERM)  (默认拒绝)
      [N+5] RET ALLOW

    Returns:
        BPF bytecode bytes, or None if not x86_64.
    """
    if platform.machine() != 'x86_64':
        logger.info("[Seccomp] Non-x86_64 platform, skipping BPF generation")
        return None

    ALLOWED = frozenset([
        # 文件 I/O (SYS_SOCKET 单独处理，需检查 AF_UNIX)
        SYS_READ, SYS_WRITE, SYS_OPEN, SYS_CLOSE, SYS_STAT, SYS_FSTAT, SYS_LSTAT,
        SYS_LSEEK, SYS_PREAD64, SYS_PWRITE64, SYS_READV, SYS_WRITEV,
        SYS_DUP, SYS_DUP2, SYS_DUP3, SYS_FCNTL, SYS_FLOCK, SYS_POLL, SYS_SELECT,
        SYS_PPOLL, SYS_PSELECT6, SYS_EPOLL_WAIT, SYS_EPOLL_CTL,
        SYS_FSYNC, SYS_FDATASYNC, SYS_FTRUNCATE, SYS_TRUNCATE,
        SYS_ACCESS, SYS_FACCESSAT, SYS_GETCWD, SYS_CHDIR, SYS_FCHDIR, SYS_GETDENTS64,
        SYS_CREAT, SYS_OPENAT, SYS_READLINK, SYS_READLINKAT,
        SYS_NEWFSTATAT, SYS_STATFS,
        SYS_MKDIR, SYS_MKDIRAT, SYS_RMDIR, SYS_UNLINK, SYS_UNLINKAT,
        SYS_RENAME, SYS_RENAMEAT, SYS_LINK, SYS_LINKAT, SYS_SYMLINK, SYS_SYMLINKAT,
        SYS_CHMOD, SYS_FCHMOD, SYS_FCHMODAT, SYS_CHOWN, SYS_FCHOWN,
        SYS_UMASK, SYS_PIPE, SYS_PIPE2,
        SYS_SENDFILE,
        SYS_UTIMENSAT,
        SYS_SIGNALFD4,
        SYS_EVENTFD2,
        SYS_TIMERFD_CREATE, SYS_TIMERFD_SETTIME, SYS_TIMERFD_GETTIME,
        SYS_TIMER_CREATE, SYS_TIMER_SETTIME,
        SYS_INOTIFY_INIT, SYS_INOTIFY_ADD_WATCH, SYS_INOTIFY_RM_WATCH,
        SYS_IOCTL,
        # 文件系统/挂载
        SYS_MOUNT, SYS_UMOUNT2, SYS_PIVOT_ROOT,
        # 内存管理
        SYS_MMAP, SYS_MUNMAP, SYS_MPROTECT, SYS_BRK, SYS_MREMAP,
        SYS_MSYNC, SYS_MADVISE, SYS_MINCORE,
        SYS_MEMFD_CREATE,
        # 进程/线程
        SYS_EXIT, SYS_EXIT_GROUP, SYS_GETPID, SYS_GETPPID, SYS_GETTID,
        SYS_GETUID, SYS_SETUID, SYS_GETGID, SYS_SETGID, SYS_GETEUID, SYS_GETEGID,
        SYS_GETPGID, SYS_GETPGRP, SYS_GETSID, SYS_SETSID, SYS_SETPGID,
        SYS_UNAME, SYS_WAIT4, SYS_CLONE, SYS_CLONE3, SYS_UNSHARE,
        SYS_EXECVE,
        SYS_SET_TID_ADDRESS, SYS_FUTEX,
        SYS_KILL, SYS_TGKILL,
        SYS_RT_SIGACTION, SYS_RT_SIGPROCMASK,
        SYS_RT_SIGSUSPEND, SYS_RT_SIGRETURN,
        SYS_GETITIMER, SYS_SETITIMER,
        SYS_NANOSLEEP, SYS_CLOCK_NANOSLEEP,
        SYS_PRCTL, SYS_ARCH_PRCTL,
        SYS_SET_ROBUST_LIST, SYS_GET_ROBUST_LIST,
        SYS_RSEQ, SYS_PERSONALITY,
        # 时间
        SYS_GETTIMEOFDAY, SYS_CLOCK_GETTIME, SYS_CLOCK_GETRES,
        SYS_TIMES, SYS_TIME,
        SYS_CAPGET, SYS_CAPSET,
        # Unix Domain Socket — --unshare-net 已阻止 AF_INET，无需 seccomp 参数检查
        SYS_SOCKET, SYS_CONNECT, SYS_ACCEPT, SYS_ACCEPT4,
        SYS_BIND, SYS_LISTEN, SYS_GETSOCKNAME, SYS_GETPEERNAME,
        SYS_SENDMSG, SYS_RECVMSG, SYS_SENDTO, SYS_RECVFROM,
        SYS_SENDMMSG, SYS_RECVMMSG,
        SYS_SETSOCKOPT, SYS_GETSOCKOPT, SYS_SHUTDOWN,
        # 调度
        SYS_SCHED_YIELD, SYS_SCHED_GETAFFINITY, SYS_SCHED_SETAFFINITY,
        SYS_SCHED_GETPARAM, SYS_SCHED_GETSCHEDULER, SYS_GETCPU,
        # 杂项
        SYS_SYSINFO, SYS_GETRUSAGE,
        SYS_PRLIMIT64, SYS_GETRLIMIT, SYS_SETRLIMIT,
        SYS_GETRANDOM,
        SYS_COPY_FILE_RANGE, SYS_GETGROUPS, SYS_SETGROUPS, SYS_SIGALTSTACK,
        SYS_SHMGET, SYS_SHMAT, SYS_SHMDT, SYS_SHMCTL,
    ])

    allowed_list = sorted(ALLOWED)
    N = len(allowed_list)

    insns = []

    # [0] LD arch
    insns.append(_build_bpf(BPF_LD | BPF_W | BPF_ABS, 0, 0, 4))
    # [1] JEQ x86_64 → skip 1 (to LD nr), else → KILL
    insns.append(_build_bpf(BPF_JMP | BPF_JEQ, 1, 0, AUDIT_ARCH_X86_64))
    # [2] RET KILL
    insns.append(_build_bpf(BPF_RET, 0, 0, SECCOMP_RET_KILL))

    # [3] LD syscall nr
    insns.append(_build_bpf(BPF_LD | BPF_W | BPF_ABS, 0, 0, 0))

    # [4..N+3] 白名单 JEQ 链
    #   ALLOW 在索引 N+5，当前索引 4+k
    #   jt = (N+5) - (4+k) - 1 = N - k
    for k, nr in enumerate(allowed_list):
        insns.append(_build_bpf(BPF_JMP | BPF_JEQ, N - k, 0, nr))

    # [N+4] 默认 ERRNO(EPERM)
    insns.append(_build_bpf(BPF_RET, 0, 0, SECCOMP_RET_ERRNO | EPERM))

    # [N+5] ALLOW
    insns.append(_build_bpf(BPF_RET, 0, 0, SECCOMP_RET_ALLOW))

    assert len(insns) == N + 6, f"BPF instruction count mismatch: {len(insns)} != {N + 6}"
    assert len(insns) < 4096, f"BPF too large: {len(insns)} instructions"

    return b"".join(insns)


# 预编译 BPF（模块加载时一次性生成）
_SECCOMP_BPF: Optional[bytes] = _create_seccomp_bpf()
_SECCOMP_BPF_B64: Optional[str] = base64.b64encode(_SECCOMP_BPF).decode() if _SECCOMP_BPF else None

# 注入到子进程 Python 的 seccomp 设置代码（在 bwrap 沙箱内安装）
# 注意：bwrap 的 NO_NEW_PRIVS 与 unshare(CLONE_NEWUSER) 不兼容，
# seccomp 必须在 bwrap 完成 user ns 创建后安装（即在沙箱内）。
# 使用 f-string 是安全的，因为 _SECCOMP_BPF_B64 只含 base64 字符。
if _SECCOMP_BPF_B64:
    SECCOMP_SETUP_CODE_SHORT = f"""
# ============================================================
# Seccomp BPF — 安装沙箱白名单（bwrap NO_NEW_PRIVS 已就绪）
# ============================================================
import ctypes as _ctypes, base64 as _b64

class _SockFprog(_ctypes.Structure):
    _fields_ = [("len", _ctypes.c_ushort), ("filter_ptr", _ctypes.c_void_p)]

_bpf_bytes = _b64.b64decode("{_SECCOMP_BPF_B64}")
_ninsns = len(_bpf_bytes) // 8
_libc = _ctypes.CDLL(None, use_errno=True)
_filter_arr = (_ctypes.c_ubyte * len(_bpf_bytes)).from_buffer_copy(_bpf_bytes)
_prog = _SockFprog()
_prog.len = _ninsns
_prog.filter_ptr = _ctypes.cast(_filter_arr, _ctypes.c_void_p)
try:
    _libc.prctl(22, 2, _ctypes.byref(_prog))  # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
except Exception:
    pass
"""
else:
    SECCOMP_SETUP_CODE_SHORT = "\n# Seccomp: skipped\n"

# _SECCOMP_SETUP_CODE 已废弃（旧版，带 SyntaxWarning）
_SECCOMP_SETUP_CODE = ""


# ============================================================================
# VFS Bootstrap Code (Core!)
# ============================================================================


# ============================================================================
# VFS Bootstrap Code (Core!)
# ============================================================================


class VFSBootstrap:
    """
    生成注入代码，在用户代码执行前先运行
    劫持 open/os/pathlib/shutil 将 /workspace/ 路径重定向到 VFS
    """

    @staticmethod
    def build(user_id: str, agent_hash: str = None,
              rate_limiter_read=None, rate_limiter_write=None,
              max_file_size: int = 100 * 1024 * 1024,
              sandbox_token: str = "") -> str:
        """
        生成 VFS monkey-patch bootstrap 代码

        VFS 通过 Unix Domain Socket 调用内部 API（不走 TCP），消除 SSRF 风险
        非 /workspace/ 路径放行到真实文件系统
        """
        # 将速率限制器参数序列化
        read_rate = settings.SANDBOX_READ_RATE_LIMIT
        write_rate = settings.SANDBOX_WRITE_RATE_LIMIT

        bootstrap = f'''
import sys, os, builtins, time, json, io, base64
from pathlib import Path as _Path

# ============================================================
# VFS Bootstrap — 在用户代码之前注入执行
# user_id={user_id}  agent_hash={agent_hash}
# ============================================================

_USER_ID = {json.dumps(user_id)}
_AGENT_HASH = {json.dumps(agent_hash)}
_VFS_API_BASE = f"http://127.0.0.1:{settings.PORT}"
_SANDBOX_TOKEN = {json.dumps(sandbox_token)}
_MAX_FILE_SIZE = {max_file_size}
_READ_RATE = {read_rate}  # bytes/sec
_WRITE_RATE = {write_rate}  # bytes/sec

# 保存原始引用
_real_open = builtins.open
_real_os_listdir = os.listdir
_real_os_scandir = os.scandir
_real_os_stat = os.stat
_real_os_mkdir = os.mkdir
_real_os_makedirs = os.makedirs
_real_os_unlink = os.unlink
_real_os_remove = os.remove
_real_os_rmdir = os.rmdir
_real_os_path_exists = os.path.exists
_real_os_path_isfile = os.path.isfile
_real_os_path_isdir = os.path.isdir
_real_os_path_getsize = os.path.getsize
_real_os_walk = os.walk
_real_os_rename = os.rename

_HTTPX_AVAILABLE = False
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    pass

# 简单的令牌桶速率限制（进程内）
class _TokenBucket:
    def __init__(self, rate, burst=None):
        self.rate = rate
        self.burst = burst or rate * 2
        self.tokens = self.burst
        self.last = time.monotonic()

    def consume(self, amount):
        now = time.monotonic()
        elapsed = now - self.last
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def wait_and_consume(self, amount):
        while not self.consume(amount):
            wait = (amount - self.tokens) / self.rate
            time.sleep(min(wait, 0.1))

_read_bucket = _TokenBucket(_READ_RATE, _READ_RATE * 5)
_write_bucket = _TokenBucket(_WRITE_RATE, _WRITE_RATE * 2)

def _is_vfs_path(path):
    """判断路径是否应该走 VFS"""
    if not isinstance(path, str):
        return False
    return path.startswith('/workspace/') or path == '/workspace'

def _vfs_path_to_api(path):
    """将 /workspace/x 映射为 VFS API 路径"""
    return path  # API 端接受 /workspace 前缀路径

def _vfs_api_call(method, endpoint, data=None):
    """通过 HTTP 调用 FeClaw 内部 VFS API"""
    import urllib.parse
    if not _HTTPX_AVAILABLE:
        raise OSError("httpx not available for VFS access")
    try:
        url = f"{{_VFS_API_BASE}}{{endpoint}}"
        sep = "&" if "?" in endpoint else "?"
        url = f"{{url}}{{sep}}agent_hash={{urllib.parse.quote(_AGENT_HASH or '')}}&token={{urllib.parse.quote(_SANDBOX_TOKEN)}}"

        with httpx.Client(timeout=30) as client:
            if method == "GET":
                r = client.get(url, timeout=30)
            else:
                r = client.request(method, url, json=data, timeout=30)

        if r.status_code == 200:
            return r.json()
        elif r.status_code == 404:
            return None
        else:
            raise OSError(f"VFS API error: {{r.status_code}}")
    except httpx.RequestError:
        raise OSError("VFS API unavailable")

# ============================================================
# Monkey-patch builtins.open
# ============================================================

def _vfs_open(file, mode='r', buffering=-1, encoding=None,
              errors=None, newline=None, closefd=True, opener=None):
    if _is_vfs_path(file):
        return _VFSFileIO(file, mode, encoding, errors)
    return _real_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

class _VFSFileIO(io.RawIOBase):
    """VFS 文件对象 — 代理到 COS-backed VFS"""

    def __init__(self, path, mode, encoding=None, errors=None):
        self._path = path
        self._mode = mode
        self._encoding = encoding or 'utf-8'
        self._errors = errors or 'replace'
        self._pos = 0
        self._dirty = False

        # 从 VFS 读取文件内容
        result = _vfs_api_call("GET", f"/api/sandbox/vfs/file?path={{path}}")
        if result and result.get("exists"):
            self._content = base64.b64decode(result["content"])
            self._size = len(self._content)
        elif 'w' in mode or 'a' in mode or '+' in mode:
            self._content = b''
            self._size = 0
        else:
            raise FileNotFoundError(f"No such file: {{path}}")
        self._size = len(self._content)

    def readable(self):
        return 'r' in self._mode or '+' in self._mode

    def writable(self):
        return 'w' in self._mode or 'a' in self._mode or '+' in self._mode

    def seekable(self):
        return True

    def read(self, size=-1):
        if not self.readable():
            raise OSError("File not open for reading")
        if size < 0:
            size = self._size - self._pos
        size = min(size, self._size - self._pos)
        if size <= 0:
            return b''
        _read_bucket.wait_and_consume(size)
        data = self._content[self._pos:self._pos + size]
        self._pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def write(self, data):
        if not self.writable():
            raise OSError("File not open for writing")
        if not isinstance(data, bytes):
            data = data.encode(self._encoding, self._errors)
        _write_bucket.wait_and_consume(len(data))
        if self._pos + len(data) > _MAX_FILE_SIZE:
            raise OSError(27, "File too large")  # EFBIG
        new_size = self._pos + len(data)
        if self._content is None:
            self._content = b'\\x00' * self._pos + data
        elif len(self._content) < new_size:
            self._content = self._content.ljust(new_size, b'\\x00')
        self._content = self._content[:self._pos] + data + self._content[new_size:]
        self._pos = new_size
        self._dirty = True
        return len(data)

    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        self._pos = max(0, min(self._pos, self._size))
        return self._pos

    def tell(self):
        return self._pos

    def truncate(self, size=None):
        if size is None:
            size = self._pos
        self._content = self._content[:size]
        self._size = len(self._content)
        self._dirty = True

    def close(self):
        # 如果被修改过，写回 VFS
        if self._dirty and self._content is not None:
            try:
                _vfs_api_call("PUT", "/api/sandbox/vfs/file", {{
                    "path": self._path,
                    "content": base64.b64encode(self._content).decode(),
                    "mode": "upload"
                }})
            except Exception as e:
                logger.warning(f"[VFS] 写回失败 {{self._path}}: {{e}}")
        self._content = None
        super().close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def readline(self, size=-1):
        if self._pos >= self._size:
            return b''
        end = self._content.find(b'\\n', self._pos)
        if end < 0:
            end = self._size
        else:
            end += 1
        if size > 0:
            end = min(end, self._pos + size)
        data = self.read(end - self._pos)
        return data

    def readlines(self, hint=-1):
        lines = []
        total = 0
        while self._pos < self._size:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint > 0 and total >= hint:
                break
        return lines

    def writelines(self, lines):
        for line in lines:
            self.write(line)

builtins.open = _vfs_open

# ============================================================
# Monkey-patch os.* operations
# ============================================================

def _vfs_listdir(path):
    if _is_vfs_path(path):
        result = _vfs_api_call("GET", f"/api/sandbox/vfs/listdir?path={{path}}")
        if result and result.get("entries"):
            return result["entries"]
        return []
    return _real_os_listdir(path)

def _vfs_scandir(path):
    if _is_vfs_path(path):
        entries = _vfs_listdir(path)
        for name in entries:
            yield _VFSScanEntry(path + '/' + name, name)
        return
    return _real_os_scandir(path)

class _VFSScanEntry:
    def __init__(self, full_path, name):
        self.name = name
        self.path = full_path
    def is_file(self):
        result = _vfs_api_call("GET", f"/api/sandbox/vfs/stat?path={{self.path}}")
        return result and not result.get("is_dir", True)
    def is_dir(self):
        result = _vfs_api_call("GET", f"/api/sandbox/vfs/stat?path={{self.path}}")
        return result and result.get("is_dir", False)
    def stat(self):
        return _vfs_stat(self.path)
    def __fspath__(self):
        return self.path

def _vfs_stat(path):
    if _is_vfs_path(path):
        result = _vfs_api_call("GET", f"/api/sandbox/vfs/stat?path={{path}}")
        if result:
            return os.stat_result((
                result.get("mode", 33188),  # st_mode
                result.get("inode", 0),     # st_ino
                0,                           # st_dev
                result.get("nlink", 1),     # st_nlink
                1000,                        # st_uid
                1000,                        # st_gid
                result.get("size", 0),      # st_size
                0,                           # st_atime (not available)
                result.get("mtime", 0),     # st_mtime
                0,                           # st_ctime (not available)
            ))
        raise FileNotFoundError(f"No such file: {{path}}")
    return _real_os_stat(path)

def _vfs_mkdir(path, mode=0o777):
    if _is_vfs_path(path):
        result = _vfs_api_call("POST", "/api/sandbox/vfs/mkdir", {{"path": path}})
        return
    return _real_os_mkdir(path, mode)

def _vfs_makedirs(path, mode=0o777, exist_ok=False):
    if _is_vfs_path(path):
        _vfs_api_call("POST", "/api/sandbox/vfs/mkdir", {{"path": path, "parents": True}})
        return
    return _real_os_makedirs(path, mode, exist_ok)

def _vfs_unlink(path):
    if _is_vfs_path(path):
        _vfs_api_call("DELETE", f"/api/sandbox/vfs/file?path={{path}}")
        return
    return _real_os_unlink(path)

def _vfs_remove(path):
    if _is_vfs_path(path):
        _vfs_api_call("DELETE", f"/api/sandbox/vfs/file?path={{path}}")
        return
    return _real_os_remove(path)

def _vfs_rmdir(path):
    if _is_vfs_path(path):
        _vfs_api_call("DELETE", f"/api/sandbox/vfs/dir?path={{path}}")
        return
    return _real_os_rmdir(path)

def _vfs_path_exists(path):
    if _is_vfs_path(path):
        try:
            _vfs_stat(path)
            return True
        except (FileNotFoundError, OSError):
            return False
    return _real_os_path_exists(path)

def _vfs_path_isfile(path):
    if _is_vfs_path(path):
        result = _vfs_api_call("GET", f"/api/sandbox/vfs/stat?path={{path}}")
        return result and not result.get("is_dir", True)
    return _real_os_path_isfile(path)

def _vfs_path_isdir(path):
    if _is_vfs_path(path):
        result = _vfs_api_call("GET", f"/api/sandbox/vfs/stat?path={{path}}")
        return result and result.get("is_dir", False)
    return _real_os_path_isdir(path)

def _vfs_path_getsize(path):
    if _is_vfs_path(path):
        result = _vfs_api_call("GET", f"/api/sandbox/vfs/stat?path={{path}}")
        if result:
            return result.get("size", 0)
        raise FileNotFoundError(f"No such file: {{path}}")
    return _real_os_path_getsize(path)

def _vfs_walk(top, topdown=True, onerror=None, followlinks=False):
    if _is_vfs_path(top):
        try:
            names = _vfs_listdir(top)
        except Exception as e:
            if onerror:
                onerror(e)
            return
        dirs, nondirs = [], []
        for name in names:
            full = top + '/' + name
            try:
                st = _vfs_stat(full)
                if st.st_mode & 0o040000:  # S_IFDIR
                    dirs.append(name)
                else:
                    nondirs.append(name)
            except OSError:
                nondirs.append(name)
            except Exception:
                nondirs.append(name)
        if topdown:
            yield top, dirs, nondirs
        for d in dirs:
            yield from _vfs_walk(top + '/' + d, topdown, onerror, followlinks)
        if not topdown:
            yield top, dirs, nondirs
        return
    return _real_os_walk(top, topdown, onerror, followlinks)

def _vfs_rename(src, dst):
    if _is_vfs_path(src) or _is_vfs_path(dst):
        _vfs_api_call("POST", "/api/sandbox/vfs/rename", {{"src": src, "dst": dst}})
        return
    return _real_os_rename(src, dst)

os.listdir = _vfs_listdir
os.scandir = _vfs_scandir
os.stat = _vfs_stat
os.mkdir = _vfs_mkdir
os.makedirs = _vfs_makedirs
os.unlink = _vfs_unlink
os.remove = _vfs_remove
os.rmdir = _vfs_rmdir
os.walk = _vfs_walk
os.rename = _vfs_rename
os.path.exists = _vfs_path_exists
os.path.isfile = _vfs_path_isfile
os.path.isdir = _vfs_path_isdir
os.path.getsize = _vfs_path_getsize

# ============================================================
# Monkey-patch pathlib.Path
# ============================================================
_RealPath = _Path

class _VFSPath(type(_RealPath())):
    def __new__(cls, *args, **kwargs):
        # 如果路径以 /workspace 开头，返回 VFS Path
        path_str = args[0] if args else kwargs.get('path', '')
        if isinstance(path_str, str) and _is_vfs_path(path_str):
            return super().__new__(cls)
        return _RealPath(*args, **kwargs)

    def read_text(self, encoding='utf-8', errors='replace'):
        p = str(self)
        if _is_vfs_path(p):
            with _vfs_open(p, 'r', encoding=encoding, errors=errors) as f:
                return f.read().decode(encoding, errors)
        return _RealPath.read_text(self, encoding, errors)

    def write_text(self, data, encoding='utf-8', errors='replace'):
        p = str(self)
        if _is_vfs_path(p):
            with _vfs_open(p, 'w', encoding=encoding, errors=errors) as f:
                f.write(data.encode(encoding, errors) if isinstance(data, str) else data)
            return
        return _RealPath.write_text(self, data, encoding, errors)

    def read_bytes(self):
        p = str(self)
        if _is_vfs_path(p):
            with _vfs_open(p, 'rb') as f:
                return f.read()
        return _RealPath.read_bytes(self)

    def write_bytes(self, data):
        p = str(self)
        if _is_vfs_path(p):
            with _vfs_open(p, 'wb') as f:
                f.write(data)
            return
        return _RealPath.write_bytes(self, data)

    def exists(self):
        p = str(self)
        if _is_vfs_path(p):
            return _vfs_path_exists(p)
        return _RealPath.exists(self)

    def is_file(self):
        p = str(self)
        if _is_vfs_path(p):
            return _vfs_path_isfile(p)
        return _RealPath.is_file(self)

    def is_dir(self):
        p = str(self)
        if _is_vfs_path(p):
            return _vfs_path_isdir(p)
        return _RealPath.is_dir(self)

    def iterdir(self):
        p = str(self)
        if _is_vfs_path(p):
            for name in _vfs_listdir(p):
                yield _RealPath(p + '/' + name)
            return
        return _RealPath.iterdir(self)

    def glob(self, pattern):
        import fnmatch as _fnmatch
        p = str(self)
        if _is_vfs_path(p):
            results = []
            for root, dirs, files in _vfs_walk(p):
                for name in dirs + files:
                    full = root + '/' + name
                    if _fnmatch.fnmatch(name, pattern) or _fnmatch.fnmatch(full, pattern):
                        results.append(_RealPath(full))
            return results
        return _RealPath.glob(self, pattern)

    def stat(self):
        p = str(self)
        if _is_vfs_path(p):
            return _vfs_stat(p)
        return _RealPath.stat(self)

    def unlink(self):
        p = str(self)
        if _is_vfs_path(p):
            _vfs_unlink(p)
            return
        return _RealPath.unlink(self)

    def mkdir(self, parents=False, exist_ok=False):
        p = str(self)
        if _is_vfs_path(p):
            if parents:
                _vfs_makedirs(p, exist_ok=exist_ok)
            else:
                _vfs_mkdir(p)
            return
        return _RealPath.mkdir(self, parents, exist_ok)

# Inject into sys.modules so 'from pathlib import Path' gets our version
sys.modules['pathlib'].Path = _VFSPath

# ============================================================
# Monkey-patch shutil (common ops)
# ============================================================
import shutil as _shutil
_real_shutil_copy = _shutil.copy
_real_shutil_copy2 = _shutil.copy2
_real_shutil_move = _shutil.move
_real_shutil_rmtree = _shutil.rmtree

def _vfs_shutil_copy(src, dst, **kwargs):
    if _is_vfs_path(src) or _is_vfs_path(dst):
        with _vfs_open(src, 'rb') as fsrc:
            data = fsrc.read()
        with _vfs_open(dst, 'wb') as fdst:
            fdst.write(data)
        return dst
    return _real_shutil_copy(src, dst, **kwargs)

def _vfs_shutil_move(src, dst, **kwargs):
    if _is_vfs_path(src) or _is_vfs_path(dst):
        _vfs_shutil_copy(src, dst)
        if _is_vfs_path(src):
            _vfs_unlink(src)
        return dst
    return _real_shutil_move(src, dst, **kwargs)

def _vfs_shutil_rmtree(path, **kwargs):
    if _is_vfs_path(path):
        _vfs_api_call("DELETE", f"/api/sandbox/vfs/dir?path={{path}}&recursive=true")
        return
    return _real_shutil_rmtree(path, **kwargs)

_shutil.copy = _vfs_shutil_copy
_shutil.copy2 = _vfs_shutil_copy  # copy2 = copy + metadata
_shutil.move = _vfs_shutil_move
_shutil.rmtree = _vfs_shutil_rmtree

# ============================================================
# Bootstrap complete — user code starts below
# ============================================================
'''.strip()
        return bootstrap


# ============================================================================
# FUSE availability cache (module-level, checked once)
# ============================================================================

_FUSE_AVAILABLE_CACHE = None


def _check_fuse_cached() -> bool:
    """缓存 check_fuse_available() 结果，避免每次 exec_code 都检查"""
    global _FUSE_AVAILABLE_CACHE
    if _FUSE_AVAILABLE_CACHE is None:
        from services.vfs_fuse_daemon import check_fuse_available
        _FUSE_AVAILABLE_CACHE = check_fuse_available()
    return _FUSE_AVAILABLE_CACHE
