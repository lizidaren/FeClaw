"""
FIX-F G4 —— pptx_tools 不再把完整 os.environ 传给 Node 子进程

审计（AUDIT-R1 §G4）：`services/tools/pptx_tools.py` 的 `_render_raster` 用
`os.environ.copy()` 后把完整服务端环境（JWT_SECRET / DATABASE_URL / 各家 API Key）
传给处理用户/Agent HTML 的 node 进程，是全仓唯一一处把完整环境泄露给子进程的地方。

修复：抽出 `_build_node_env(chrome)`，只注入 node + Playwright Chromium 必需的
PATH/HOME/LANG/LC_ALL + CHROME 路径，对齐 FIX-C bwrap `env -i` 的最小 env 做法。
"""
import pytest


class TestNodeEnvWhitelist:
    def test_env_excludes_secrets(self, monkeypatch):
        from services.tools.pptx_tools import _build_node_env

        # 模拟服务端环境里真实存在的敏感变量
        monkeypatch.setenv("JWT_SECRET", "topsecret")
        monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@localhost/db")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/lch")

        env = _build_node_env("/usr/bin/chrome")

        assert set(env.keys()) <= {"PATH", "HOME", "LANG", "LC_ALL", "CHROME"}
        assert "JWT_SECRET" not in env
        assert "DATABASE_URL" not in env
        assert "DEEPSEEK_API_KEY" not in env
        # 必需变量仍注入
        assert env["CHROME"] == "/usr/bin/chrome"
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/home/lch"

    def test_env_defaults_when_unset(self, monkeypatch):
        from services.tools.pptx_tools import _build_node_env

        monkeypatch.delenv("PATH", raising=False)
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)

        env = _build_node_env("/opt/chrome")

        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/tmp"
        assert env["LANG"] == "C.UTF-8"
        assert env["LC_ALL"] == "C.UTF-8"
