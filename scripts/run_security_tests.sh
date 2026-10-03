#!/bin/bash
# FeClaw 安全回归测试入口（N24：给「没有可跑入口」的回归套件补一条稳定命令）
#
# 用法：
#   scripts/run_security_tests.sh                 # 全量回归（与基线 41/1118/2 同一条命令）
#   scripts/run_security_tests.sh -k route_authz  # 透传任意 pytest 参数（子集/关键字）
#   PYTHON=/path/to/python scripts/run_security_tests.sh
#
# 为什么需要它：
#   1) requirements.txt 无 pytest/pytest-asyncio —— 见 requirements-dev.txt；
#   2) 若干个**预存在**的收集错误文件（导入已删除符号 / FUSE 压测）若不忽略，
#      整套 pytest 会在收集阶段就炸掉，任何人也跑不起来。
#
# 既有 ignore 清单（各批次报告已确认，均为基线既存，非本次改动引入）：
#   tests/rerank                   —— 导入已删除的 EMBEDDING_API_URL（vector_search_service）
#   tests/test_subagent_presets.py / _api.py / test_subagent_summary.py —— 引用已删除的 PRESET_ROLES
#   tests/test_zentrim_api.py      —— 预存在错误
#   tests/stress_test.py           —— FUSE 压测（*_test.py 会被 pytest 收集，顶层可执行代码 + 依赖 trio）
#
# pyfuse3/trio 处理：
#   services/vfs_fuse_daemon.py:1068 有**未保护**的模块级 `import trio`，
#   tests/test_fuse_mount.py 模块级 import 它 ⇒ 本机缺 trio 时整套收集失败。
#   下面自动探测：缺 pyfuse3/trio 就额外忽略 test_fuse_mount.py（fail-safe 降级），
#   装了则可全跑。

set -uo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

IGNORES=(
  --ignore=tests/rerank
  --ignore=tests/test_subagent_presets.py
  --ignore=tests/test_subagent_presets_api.py
  --ignore=tests/test_subagent_summary.py
  --ignore=tests/test_zentrim_api.py
  --ignore=tests/stress_test.py
)

# 缺 pyfuse3 或 trio ⇒ 额外忽略 test_fuse_mount.py（否则收集阶段 ImportError 炸整套）
if ! "$PYTHON" -c "import pyfuse3, trio" >/dev/null 2>&1; then
  echo "[run_security_tests] pyfuse3/trio 不可用 → 额外忽略 tests/test_fuse_mount.py（FUSE 挂载测试）" >&2
  IGNORES+=( --ignore=tests/test_fuse_mount.py )
fi

exec "$PYTHON" -m pytest tests/ -q --tb=line -p no:warnings "${IGNORES[@]}" "$@"
