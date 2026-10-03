"""模板内联 JS 语法守卫（FIX-I）

背景：FIX-D 在 templates/agent_configure.html 的同一作用域里重复声明了
`const applyBtn`，导致该 <script> 块整块 SyntaxError，页面上「保存 / 完成初始化」
全部点了没反应，而既有的 Python 回归测试**完全抓不到**（它们不解析模板 JS）。

本测试把每个模板里的内联 <script> 抽出来做真实语法检查（node --check）。
- 无 node 时 skip（CI/环境缺失不应误报 ✗）
- Jinja 表达式替换为占位后再校验 ✓
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, "templates")
SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _blocks(path):
    html = open(path, encoding="utf-8").read()
    for i, m in enumerate(SCRIPT_RE.finditer(html)):
        js = re.sub(r"\{\{.*?\}\}", "null", m.group(1), flags=re.S)
        js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
        yield i, js


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 才能校验 JS 语法")
def test_inline_js_syntax():
    bad = []
    tpls = sorted(f for f in os.listdir(TPL_DIR) if f.endswith(".html"))
    assert tpls, "没找到任何模板，路径可能不对"
    for name in tpls:
        path = os.path.join(TPL_DIR, name)
        for idx, js in _blocks(path):
            tmp = "/tmp/_tpl_js_%s_%d.js" % (name.replace("/", "_"), idx)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(js)
            r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
            if r.returncode != 0:
                first = (r.stderr or "").strip().split("\n")
                bad.append("%s script#%d: %s" % (name, idx, first[0] if first else "?"))
    assert not bad, "模板内联 JS 有语法错误：\n" + "\n".join(bad)
