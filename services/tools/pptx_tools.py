"""
PPTX Agent 工具 — HTML 幻灯片 → PowerPoint

Agent 写 HTML 幻灯片（deck-slide 格式）→ html2pptx 转换 → 保存到 VFS → 返回分享链接.

需要服务器安装：
- python-pptx
- playwright + chromium
- node.js (for html2pptx SVG extraction)
- libatk-bridge2.0-0 libgbm1 libatspi2.0-0 (Chromium 依赖)
"""

import os
import json
import uuid
import logging
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

from services.tools.base import AgentToolsServiceBase, tool

logger = logging.getLogger(__name__)

# html2pptx 脚本路径（项目内嵌）
_SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_HTML2PPTX_SCRIPT = _SCRIPT_DIR / "html2pptx" / "html2pptx.py"
_CHROMIUM_PATH = os.environ.get(
    "CHROME",
    os.path.expanduser("~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"),
)

# 如果不存在就找 latest
if not os.path.exists(str(_CHROMIUM_PATH)):
    _playwright_dir = Path.home() / ".cache" / "ms-playwright"
    if _playwright_dir.exists():
        _chromium_dirs = sorted(_playwright_dir.glob("chromium-*/chrome-linux64/chrome"))
        if _chromium_dirs:
            _CHROMIUM_PATH = str(_chromium_dirs[-1])


class PptxToolsMixin(AgentToolsServiceBase):
    """PPTX 生成工具 Mixin"""

    @tool(description="将 HTML 幻灯片转换为 PowerPoint 文件", category="file")
    def create_pptx(
        self,
        html_content: str,
        filename: str = "presentation.pptx",
    ) -> str:
        """
        将 HTML 幻灯片转换为 PPTX

        Args:
            html_content: 完整的 HTML 文档。每张幻灯片用 <section class="deck-slide"> 包裹。
                         需要包含 <style> 定义样式。参考：
                         <!DOCTYPE html><html><head><style>
                         .deck-slide { width:1280px; height:720px; ... }
                         </style></head><body>
                         <section class="deck-slide">...</section>
                         </body></html>
            filename: 输出文件名（默认 presentation.pptx）

        Returns:
            成功返回 VFS 路径和分享链接，失败返回错误信息
        """
        try:
            # 1. 写入临时 HTML 文件
            html_path = os.path.join(
                tempfile.gettempdir(),
                f"pptx_input_{uuid.uuid4().hex[:8]}.html",
            )
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            # 2. 确定输出路径
            output_path = os.path.join(
                tempfile.gettempdir(),
                f"pptx_output_{uuid.uuid4().hex[:8]}.pptx",
            )

            # 3. 检查 html2pptx 脚本是否存在
            script = _SCRIPT_DIR / "html2pptx" / "html2pptx.py"
            if not script.exists():
                return "Error: html2pptx 转换脚本未安装"

            chrome = str(_CHROMIUM_PATH)
            if not os.path.exists(chrome):
                return "Error: Chromium 未安装。请先运行 playwright install chromium"

            # 4. 执行转换
            cmd = [
                "python3", str(script),
                str(html_path),
                "-o", str(output_path),
                "--chrome", chrome,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )

            if result.returncode != 0:
                logger.warning(f"html2pptx failed: {result.stderr[:500]}")
                return f"Error: PPTX 转换失败: {result.stderr[:200]}"

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return "Error: PPTX 生成失败（输出为空）"

            # 5. 读取 PPTX 内容并保存到 VFS
            with open(output_path, "rb") as f:
                pptx_bytes = f.read()

            vfs_path = f"workspace/presentations/{filename}"
            abs_key = f"feclaw/agents/{self.agent_hash}/{vfs_path}"
            from services.storage_service import StorageService
            StorageService().upload_file(pptx_bytes, abs_key)

            # 6. 生成分享链接
            share_result = None
            try:
                from services.share_service import create_share_link
                share_result = create_share_link(
                    vfs_path=f"/{vfs_path}",
                    mode="share",
                    user_id=self.user_id,
                    expires_hours=24 * 30,  # 30天
                    agent_hash=self.agent_hash,
                )
            except Exception as e:
                logger.warning(f"Share link generation failed: {e}")

            # 7. 清理临时文件
            try:
                os.unlink(html_path)
                os.unlink(output_path)
            except OSError:
                pass

            # 8. 返回结果
            result_parts = [
                f"✅ PPTX 已生成并保存到 {vfs_path}",
                f"大小: {len(pptx_bytes) / 1024:.0f} KB",
            ]
            if share_result:
                result_parts.append(f"分享链接: {share_result['url']}")
            return "\n".join(result_parts)

        except subprocess.TimeoutExpired:
            return "Error: PPTX 转换超时（>60 秒）"
        except Exception as e:
            logger.warning(f"create_pptx failed: {e}")
            return f"Error: PPTX 生成失败: {str(e)[:200]}"
