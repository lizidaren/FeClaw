"""T19a：可重现依赖 + 可选模块真的可选（可选功能缺失不炸启动）。

本文件只验证**依赖层面**的契约，不验证干净 venv 的完整安装矩阵（那是 T19b）：

1. 缺 trio / pyfuse3 时 ``services.vfs_fuse_daemon`` 可正常 import，
   ``_FUSE_AVAILABLE is False``，挂载入口抛明确的 ``RuntimeError``（不是 ``NameError``），
   并且失败发生在任何副作用（建目录、起线程、调 fusermount3）之前。
   —— 用子进程 + ``sys.meta_path`` 拦截器模拟「没有装这个包」，不依赖本机是否真的装了。
2. 可选模块（PDF/DOCX/PPTX）缺失时，解析器返回「未安装」的明确提示，而不是抛 ImportError。
   —— 现有实现是函数内 ``import fitz`` + ``except ImportError``，本测试把该契约钉死。
3. ``requirements.lock`` 存在、每行可解析、无重复包名、全部是 ``==`` 精确锁定。
4. 声明层（requirements*.txt）与锁定层（requirements.lock）一致：
   声明过的包必须在 lock 里，除非在声明文件里显式写了 ``# lock-pending: <name>`` 说明
   本机无法验证；lock 里非 transitive 分组的包必须都能在声明层找到出处。
5. 分组校验：``fuse`` 组只含 trio/pyfuse3，``core``/``optional``/``test`` 不含 FUSE 包。
6. 「未使用依赖」的处理是**静态核实**过的，不是凭印象：sse-starlette / eval_type_backport
   既不在声明层也不在锁里，且全仓库确实没有 import。
7. 锁定的 starlette 版本满足 fastapi 的元数据约束（防止 sse-starlette 那类冲突回归）。
8. Dockerfile 从 requirements.lock 安装；pyproject 的 fuse extra 与 lock 的 fuse 组一致。

**未覆盖（T19b）**：干净 venv 最小安装、缺 helper/model key/Redis 的启动矩阵、FUSE 真实挂载。
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "requirements.lock"
DECLARATION_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-fuse.txt")

KNOWN_GROUPS = {"core", "optional", "fuse", "test", "transitive"}
PINNED_GROUPS = {"core", "optional", "fuse", "test"}
FUSE_PACKAGES = {"trio", "pyfuse3"}
UNUSED_REMOVED = {"sse-starlette", "eval-type-backport"}

GROUP_RE = re.compile(r"^#\s*===\s*group:\s*([a-z-]+)\s*===\s*$")
PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[^\]]+)\])?"
    r"==(?P<ver>[A-Za-z0-9][A-Za-z0-9._+!-]*)$"
)
PENDING_RE = re.compile(r"^#\s*lock-pending:\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
DECL_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?")


def _norm(name: str) -> str:
    """PEP 503 归一化：``-``/``_``/``.`` 等价、大小写不敏感。"""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _strip_inline_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


# ───────────────────────────── 解析器 ─────────────────────────────

def parse_lock() -> dict[str, list[tuple[str, str, int]]]:
    """解析 requirements.lock → {group: [(name, version, lineno), ...]}。"""
    assert LOCK_PATH.exists(), f"缺少 {LOCK_PATH.name}（T19a 要求唯一的锁定清单）"
    groups: dict[str, list[tuple[str, str, int]]] = {}
    current: str | None = None
    for lineno, raw in enumerate(LOCK_PATH.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = GROUP_RE.match(line)
            if m:
                current = m.group(1)
                groups.setdefault(current, [])
            continue
        body = _strip_inline_comment(line)
        if not body:
            continue
        assert current is not None, f"requirements.lock:{lineno} 的包 {body!r} 不在任何 group 里"
        m = PIN_RE.match(body)
        assert m, (
            f"requirements.lock:{lineno} 无法解析为 name[extras]==version: {raw!r}"
            "（lock 里不允许 >= / ~= / 范围）"
        )
        groups[current].append((m.group("name"), m.group("ver"), lineno))
    assert groups, "requirements.lock 里没有解析出任何分组"
    return groups


def lock_pins() -> dict[str, str]:
    """{归一化包名: version}，覆盖所有分组。"""
    pins: dict[str, str] = {}
    for entries in parse_lock().values():
        for name, version, _ in entries:
            pins[_norm(name)] = version
    return pins


def parse_declarations() -> tuple[dict[str, str], set[str]]:
    """解析 requirements*.txt → ({归一化包名: 来源文件}, {lock-pending 包名})。"""
    declared: dict[str, str] = {}
    pending: set[str] = set()
    for filename in DECLARATION_FILES:
        path = REPO_ROOT / filename
        assert path.exists(), f"缺少声明文件 {filename}"
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            m = PENDING_RE.match(line)
            if m:
                pending.add(_norm(m.group(1)))
                continue
            if line.startswith("#") or line.startswith("-"):
                continue
            body = _strip_inline_comment(line).split(";", 1)[0].strip()
            if not body:
                continue
            m = DECL_RE.match(body)
            assert m, f"{filename}:{lineno} 无法解析依赖声明: {raw!r}"
            declared[_norm(m.group("name"))] = filename
    assert declared, "requirements*.txt 里没有解析出任何依赖声明"
    return declared, pending


def iter_repo_py_files() -> list[Path]:
    skip_dirs = {".venv", ".git", "__pycache__", "_archive", "node_modules", ".tmp", "test-results"}
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & skip_dirs:
            continue
        out.append(path)
    return out


def third_party_imports() -> dict[str, list[str]]:
    """{top-level module: [文件, ...]}，排除 stdlib 与本仓库一级包。"""
    import sys as _sys

    std = set(_sys.stdlib_module_names)
    first_party = {
        "config", "main", "models", "routers", "services", "utils", "scripts", "tests", "__init__",
    }
    found: dict[str, list[str]] = {}
    for path in iter_repo_py_files():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # 预存在的坏文件不阻断依赖审计
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in std and top not in first_party:
                        found.setdefault(top, []).append(rel)
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                top = node.module.split(".")[0]
                if top not in std and top not in first_party:
                    found.setdefault(top, []).append(rel)
    return found


# ─────────────────────── 子进程「缺包」模拟器 ───────────────────────

_BLOCKER_BOOTSTRAP = '''
import sys
import importlib.abc


class _S6MissingDependencyBlocker(importlib.abc.MetaPathFinder):
    """让指定顶层模块的 import 表现为「没有安装」。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _S6_BLOCKED:
            raise ModuleNotFoundError("No module named %r" % (fullname,), name=fullname)
        return None


sys.meta_path.insert(0, _S6MissingDependencyBlocker())
'''


def run_without_dependencies(blocked, body, extra_env=None):
    """在子进程里屏蔽 blocked 的顶层模块，然后执行 body（必须打印 S6RESULT<json>）。"""
    code = "_S6_BLOCKED = %r\n" % (sorted(blocked),)
    code += textwrap.dedent(_BLOCKER_BOOTSTRAP)
    code += textwrap.dedent(body)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("SETUP_COMPLETE", "true")
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def result_of(proc) -> dict:
    for line in (proc.stdout or "").splitlines():
        if line.startswith("S6RESULT"):
            return json.loads(line[len("S6RESULT"):])
    raise AssertionError(
        "子进程没有输出 S6RESULT\n"
        f"returncode={proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


_FUSE_PROBE = '''
import json
import os

import services.vfs_fuse_daemon as daemon

probe = os.path.join(os.environ["S6_PROBE_DIR"], "mount")
result = {
    "trio": daemon._TRIO_AVAILABLE,
    "pyfuse3": daemon._PYFUSE3_AVAILABLE,
    "fuse": daemon._FUSE_AVAILABLE,
    "check": daemon.check_fuse_available(),
    "reason": daemon.fuse_unavailable_reason(),
}
try:
    daemon.start_fuse_background(None, probe)
    result["raised"] = None
    result["message"] = ""
except RuntimeError as exc:
    result["raised"] = "RuntimeError"
    result["message"] = str(exc)
except BaseException as exc:  # NameError/ImportError 都算失败
    result["raised"] = type(exc).__name__
    result["message"] = str(exc)
result["probe_dir_created"] = os.path.isdir(probe)
print("S6RESULT" + json.dumps(result))
'''


# ───────────────────────── 1) FUSE 可选依赖 guard ─────────────────────────

def test_fuse_module_imports_without_trio_and_pyfuse3(tmp_path):
    """缺 trio + pyfuse3：模块可 import、_FUSE_AVAILABLE=False、挂载入口报明确错误。"""
    proc = run_without_dependencies(
        {"trio", "pyfuse3"}, _FUSE_PROBE, {"S6_PROBE_DIR": str(tmp_path)}
    )
    assert proc.returncode == 0, f"缺 trio/pyfuse3 时 import 就崩了：\n{proc.stderr}"
    data = result_of(proc)

    assert data["trio"] is False
    assert data["pyfuse3"] is False
    assert data["fuse"] is False
    assert data["check"] is False
    assert data["raised"] == "RuntimeError", f"挂载入口应抛 RuntimeError，实际 {data['raised']}"
    assert "trio" in data["message"] and "pyfuse3" in data["message"], data["message"]
    assert "NameError" not in data["message"]
    # 失败必须发生在副作用之前：不能先建目录/起线程再报错
    assert data["probe_dir_created"] is False


def test_fuse_module_imports_without_trio_only(tmp_path):
    """只缺 trio（pyfuse3 在）——证明新加的 trio guard 独立生效。

    本机（Windows）没有 pyfuse3，此时「只缺 trio」与「两者都缺」等价，
    结果已由 test_fuse_module_imports_without_trio_and_pyfuse3 覆盖；
    在装有 pyfuse3 的 Linux 上这条才有独立信息量，避免重复起子进程。
    """
    import services.vfs_fuse_daemon as daemon

    if not daemon._PYFUSE3_AVAILABLE:
        pytest.skip("本机未安装 pyfuse3：trio-only 场景与 both-blocked 子进程用例等价")

    proc = run_without_dependencies(
        {"trio"}, _FUSE_PROBE, {"S6_PROBE_DIR": str(tmp_path)}
    )
    assert proc.returncode == 0, f"缺 trio 时 import 就崩了：\n{proc.stderr}"
    data = result_of(proc)
    assert data["trio"] is False
    assert data["pyfuse3"] is True, "该用例要求 pyfuse3 已安装，才能证明只是 trio 缺失"
    assert data["fuse"] is False
    assert data["raised"] == "RuntimeError"
    assert "trio" in data["message"]


def test_fuse_module_imports_without_pyfuse3_only(tmp_path):
    """缺 pyfuse3（trio 在）——「Linux 没装 fuse3 / Windows 本机」的真实形态。

    不走子进程：本机 venv 就是 trio 装着、pyfuse3 没装，属于天然正例；
    这里顺便断言挂载入口抛 RuntimeError（而不是 NameError / AttributeError）。
    """
    import services.vfs_fuse_daemon as daemon

    assert daemon._TRIO_AVAILABLE is True, (
        "requirements-dev.txt/venv 里应装有 trio；若本机没有，请用 "
        "test_fuse_module_imports_without_trio_and_pyfuse3 的子进程用例覆盖"
    )
    assert daemon._PYFUSE3_AVAILABLE is False, (
        "本机（Windows）不应装上 pyfuse3；若装上了，请改走子进程屏蔽用例"
    )
    assert daemon._FUSE_AVAILABLE is False
    assert daemon.check_fuse_available() is False

    probe = tmp_path / "mount"
    with pytest.raises(RuntimeError) as excinfo:
        daemon.start_fuse_background(None, str(probe))
    message = str(excinfo.value)
    assert "pyfuse3" in message, message
    assert "NameError" not in message
    assert not probe.exists(), "挂载入口必须在产生副作用（建目录/起线程）之前就失败"


def test_fuse_flags_are_consistent_in_this_process():
    """正例对照：本进程（缺 pyfuse3 的 Windows）里 flag 与 check 必须自洽。"""
    import services.vfs_fuse_daemon as daemon

    assert daemon._FUSE_AVAILABLE is (
        daemon._PYFUSE3_AVAILABLE and daemon._TRIO_AVAILABLE
    )
    if not daemon._FUSE_AVAILABLE:
        assert daemon.check_fuse_available() is False
        reason = daemon.fuse_unavailable_reason()
        assert ("pyfuse3" in reason) or ("trio" in reason) or ("/dev/fuse" in reason)


# ─────────────────── 2) 可选模块缺失 → 明确降级，不炸 ───────────────────

_OPTIONAL_PARSER_PROBE = '''
import json

from services.tools import universal_parser

out = {}
for ext in ("pdf", "docx", "pptx"):
    try:
        out[ext] = {"raised": None, "value": universal_parser._extract_text("missing." + ext)}
    except BaseException as exc:
        out[ext] = {"raised": type(exc).__name__, "value": str(exc)}
print("S6RESULT" + json.dumps(out))
'''


def test_optional_parser_reports_unavailable_instead_of_crashing():
    """fitz/python-docx/python-pptx 缺失时返回「未安装」，而不是抛 ImportError。"""
    proc = run_without_dependencies({"fitz", "docx", "pptx"}, _OPTIONAL_PARSER_PROBE)
    assert proc.returncode == 0, f"可选解析器 import 阶段就炸了：\n{proc.stderr}"
    data = result_of(proc)

    for ext, package in (("pdf", "PyMuPDF"), ("docx", "python-docx"), ("pptx", "python-pptx")):
        assert data[ext]["raised"] is None, f"{ext} 抛了 {data[ext]['raised']}: {data[ext]['value']}"
        assert package in data[ext]["value"], (ext, data[ext]["value"])
        assert "未安装" in data[ext]["value"], (ext, data[ext]["value"])


# ─────────────────────── 3) requirements.lock 结构 ───────────────────────

def test_lock_exists_and_every_line_parses():
    groups = parse_lock()  # 内部断言每个包行都能解析
    assert LOCK_PATH.is_file()
    assert sum(len(v) for v in groups.values()) > 0
    assert KNOWN_GROUPS.issubset(set(groups)), f"缺少分组: {KNOWN_GROUPS - set(groups)}"


def test_lock_has_no_duplicate_package_across_groups():
    seen: dict[str, tuple[str, int]] = {}
    duplicates = []
    for group, entries in parse_lock().items():
        for name, _version, lineno in entries:
            key = _norm(name)
            if key in seen:
                duplicates.append((key, seen[key], (group, lineno)))
            else:
                seen[key] = (group, lineno)
    assert not duplicates, f"requirements.lock 里包名重复（会形成两套锁）: {duplicates}"


def test_lock_uses_exact_pins_only():
    text = LOCK_PATH.read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = _strip_inline_comment(line)
        assert ">=" not in body and "<=" not in body and "~=" not in body and "*" not in body, (
            f"requirements.lock:{lineno} 出现范围约束，lock 必须是精确版本: {raw!r}"
        )
        assert body.count("==") == 1, f"requirements.lock:{lineno} 不是单一 ==pin: {raw!r}"


# ─────────────── 4) 声明层 vs 锁定层的一致性 ───────────────

def test_declared_requirements_are_locked_or_explicitly_pending():
    declared, pending = parse_declarations()
    pins = lock_pins()
    missing = sorted(name for name in declared if name not in pending and name not in pins)
    assert not missing, (
        "requirements*.txt 声明了但 requirements.lock 没锁定（也不是 lock-pending）: "
        f"{missing}"
    )


def test_lock_pending_is_not_also_pinned():
    declared, pending = parse_declarations()
    pins = lock_pins()
    overlap = sorted(pending & set(pins))
    assert not overlap, f"既写 lock-pending 又出现在 lock 里: {overlap}"
    # lock-pending 必须真的在声明层出现过，不能凭空写
    assert pending, "没有解析到任何 lock-pending 标注"
    assert pending <= set(declared), f"lock-pending 的包没在声明里: {pending - set(declared)}"


def test_lock_direct_groups_are_declared_in_requirements_files():
    declared, _pending = parse_declarations()
    pins = lock_pins()
    undeclared = []
    for group in PINNED_GROUPS:
        for name, _version, _lineno in parse_lock().get(group, []):
            if _norm(name) not in declared:
                undeclared.append((group, name))
    assert not undeclared, (
        f"lock 的 {sorted(PINNED_GROUPS)} 分组里有包没在 requirements*.txt 声明: {undeclared}"
    )
    # lock 允许是声明层的**超集**：多出来的部分必须都在 transitive 组里
    transitive = {_norm(n) for n, _v, _l in parse_lock().get("transitive", [])}
    extra = set(pins) - set(declared)
    assert extra <= transitive, f"lock 里多出来的包必须在 transitive 组: {extra - transitive}"


def test_lock_and_requirements_cross_reference_each_other():
    requirements_txt = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock_text = LOCK_PATH.read_text(encoding="utf-8")
    assert "requirements.lock" in requirements_txt, "requirements.txt 头部必须指向 lock（避免两套锁）"
    assert "requirements.txt" in lock_text, "lock 头部必须说明与 requirements.txt 的关系"


# ───────────────────────── 5) 分组校验 ─────────────────────────

def test_fuse_group_is_isolated():
    groups = parse_lock()
    fuse_names = {_norm(n) for n, _v, _l in groups.get("fuse", [])}
    assert fuse_names, "fuse 分组为空：trio 必须被锁定"
    assert fuse_names <= FUSE_PACKAGES, f"fuse 组只允许 trio/pyfuse3，实际: {fuse_names}"
    assert "trio" in fuse_names, "FUSE 组必须含 trio（pyfuse3 的事件循环）"

    for group in ("core", "optional", "test", "transitive"):
        leaked = {_norm(n) for n, _v, _l in groups.get(group, [])} & FUSE_PACKAGES
        assert not leaked, f"{group} 组不能包含 FUSE 包: {leaked}"

    fuse_decl = (REPO_ROOT / "requirements-fuse.txt").read_text(encoding="utf-8").lower()
    assert "trio" in fuse_decl and "pyfuse3" in fuse_decl, "requirements-fuse.txt 必须声明 trio + pyfuse3"

    dev_decl = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
    dev_lines = [
        _norm(m.group(1))
        for line in dev_decl.splitlines()
        if (m := DECL_RE.match(_strip_inline_comment(line.strip())))
    ]
    assert "trio" not in dev_lines, "trio 属于 FUSE 组，不应再在 requirements-dev.txt 里声明"


def test_test_group_holds_the_test_framework():
    groups = parse_lock()
    test_names = {_norm(n) for n, _v, _l in groups.get("test", [])}
    assert "pytest" in test_names
    assert "pytest-asyncio" in test_names


# ─────────────── 6) 未使用依赖的删除决定（静态核实） ───────────────

def test_unused_dependencies_are_neither_declared_nor_locked():
    declared, pending = parse_declarations()
    pins = lock_pins()

    for name in UNUSED_REMOVED:
        assert name not in declared, f"{name} 已确认未被 import，不应再出现在 requirements*.txt"
        assert name not in pending, f"{name} 不应被标成 lock-pending"
        assert name not in pins, f"{name} 不应出现在 requirements.lock（会带回版本冲突）"

    imports = third_party_imports()
    assert "sse_starlette" not in imports, (
        f"仓库开始 import sse_starlette 了（{imports.get('sse_starlette')}）——"
        "请重新评估与 starlette 0.41 的版本冲突，而不是恢复依赖"
    )
    assert "eval_type_backport" not in imports

    # EventSourceResponse 只作为文本出现也不允许（说明有运行期动态 import 的迹象）。
    # 本测试文件自己在断言里写了这个字符串，扫描时按解析后的绝对路径排除。
    own_file = Path(__file__).resolve()
    hits = []
    for path in iter_repo_py_files():
        try:
            is_self = path.resolve() == own_file
        except OSError:
            is_self = False
        if is_self:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "EventSourceResponse" in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert not hits, f"发现 EventSourceResponse 使用点: {hits}"


# ─────────────── 7) 锁定版本满足框架元数据约束 ───────────────

def test_locked_starlette_satisfies_fastapi_metadata():
    """sse-starlette 3.5.1 与 starlette 0.41.3 的冲突不能通过锁文件回归。"""
    from importlib import metadata

    packaging_requirements = pytest.importorskip("packaging.requirements")
    packaging_version = pytest.importorskip("packaging.version")

    try:
        requires = metadata.requires("fastapi") or []
    except metadata.PackageNotFoundError:
        pytest.skip("本环境没装 fastapi，跳过元数据一致性检查")

    starlette_reqs = [
        r for r in requires if r.split(";")[0].strip().lower().startswith("starlette")
    ]
    assert starlette_reqs, "fastapi 的元数据里没有 starlette 约束？请人工复核"

    locked = lock_pins().get("starlette")
    assert locked, "requirements.lock 必须锁定 starlette（main.py 直接 import 它）"

    for raw in starlette_reqs:
        spec = packaging_requirements.Requirement(raw).specifier
        assert spec.contains(packaging_version.Version(locked), prereleases=True), (
            f"锁定的 starlette=={locked} 不满足 fastapi 的约束 {raw!r}；"
            "升级 starlette 必须同时升级 fastapi 并重跑测试"
        )


# ─────────────── 8) 构建/打包文件与锁保持一致 ───────────────

def test_dockerfile_installs_from_lock_and_not_unpinned_packages():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    body_lines = [
        line for line in dockerfile.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    body = "\n".join(body_lines)
    assert "requirements.lock" in body, "Dockerfile 必须从 requirements.lock 安装"
    assert re.search(r"pip install[^\n]*-r requirements\.lock", body), "Dockerfile 的 pip 安装行必须用 lock"
    assert "eval_type_backport" not in body, "Dockerfile 不应再单独安装未使用/未锁定的包"
    assert "requirements.txt" not in body, "Dockerfile 不应再从范围声明层安装（会绕过锁）"


def test_pyproject_fuse_extra_matches_lock_fuse_group():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["dependencies"] == [], (
        "pyproject 不应重复声明运行时依赖；唯一权威是 requirements.lock + requirements.txt"
    )

    extra = data["project"].get("optional-dependencies", {}).get("fuse")
    assert extra, "pyproject 必须有 [project.optional-dependencies] fuse extra"

    def _pins(items):
        out = set()
        for item in items:
            m = PIN_RE.match(item.strip())
            assert m, f"无法解析 pin: {item!r}"
            out.add((_norm(m.group("name")), m.group("ver")))
        return out

    lock_fuse = {(name, version) for name, version, _ in parse_lock()["fuse"]}
    assert _pins(extra) == {(_norm(n), v) for n, v in lock_fuse}, (
        "pyproject 的 fuse extra 与 requirements.lock 的 fuse 分组不一致（两处版本漂移）"
    )
