#!/usr/bin/env python3
"""
/public/ 公共数据空间管理工具

用法:
    python scripts/public_manager.py list                    # 列出所有公共文件
    python scripts/public_manager.py upload <local> <remote> # 上传文件
    python scripts/public_manager.py download <remote> [local] # 下载文件
    python scripts/public_manager.py delete <remote>         # 删除文件
    python scripts/public_manager.py read <remote>           # 读取文件内容
    python scripts/public_manager.py validate                # 验证公共文件完整性
    python scripts/public_manager.py init                    # 初始化默认公共文件

示例:
    python scripts/public_manager.py upload ./docs/index.md feclaw/index.md
    python scripts/public_manager.py list feclaw/tools/
    python scripts/public_manager.py read feclaw/index.md
    python scripts/public_manager.py validate
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from services.storage_service import StorageService


class PublicFileManager:
    """公共文件管理器"""
    
    def __init__(self):
        self.storage = StorageService()
        self.public_prefix = f"{settings.TENCENT_COS_PREFIX}public/"
    
    def list_files(self, prefix: str = "") -> list:
        """
        列出公共目录下的文件
        
        Args:
            prefix: 相对于 /public/ 的路径前缀，如 "feclaw/tools/"
        
        Returns:
            文件信息列表
        """
        cos_prefix = self.public_prefix + prefix.lstrip("/")
        if not cos_prefix.endswith("/"):
            cos_prefix += "/"
        
        try:
            result = self.storage.list_objects(cos_prefix, max_keys=1000)

            files = []
            if result:
                for obj in result:
                    key = obj["Key"]
                    rel_path = key[len(self.public_prefix):]
                    files.append({
                        "path": rel_path,
                        "size": obj.get("Size", 0),
                        "last_modified": obj.get("LastModified", ""),
                        "etag": obj.get("ETag", "")
                    })
            
            return files
        except Exception as e:
            print(f"Error: {e}")
            return []
    
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """
        上传文件到公共目录
        
        Args:
            local_path: 本地文件路径
            remote_path: 远程路径（相对于 /public/），如 "feclaw/index.md"
        
        Returns:
            是否成功
        """
        if not os.path.exists(local_path):
            print(f"Error: 本地文件不存在: {local_path}")
            return False
        
        cos_key = self.public_prefix + remote_path.lstrip("/")
        
        try:
            with open(local_path, "rb") as f:
                content = f.read()
            
            self.storage.put_object(cos_key, content)
            print(f"✅ 上传成功: {local_path} → /public/{remote_path}")
            return True
        except Exception as e:
            print(f"Error: 上传失败: {e}")
            return False
    
    def download_file(self, remote_path: str, local_path: str = None) -> bool:
        """
        从公共目录下载文件
        
        Args:
            remote_path: 远程路径（相对于 /public/），如 "feclaw/index.md"
            local_path: 本地保存路径（可选，默认保存到当前目录）
        
        Returns:
            是否成功
        """
        cos_key = self.public_prefix + remote_path.lstrip("/")
        
        if local_path is None:
            local_path = os.path.basename(remote_path)
        
        try:
            content = self.storage.get_file_content(cos_key)
            if content is None:
                print(f"Error: 文件不存在: /public/{remote_path}")
                return False
            
            # 确保目录存在
            os.makedirs(os.path.dirname(local_path) if os.path.dirname(local_path) else ".", exist_ok=True)
            
            with open(local_path, "wb") as f:
                f.write(content)
            
            print(f"✅ 下载成功: /public/{remote_path} → {local_path}")
            return True
        except Exception as e:
            print(f"Error: 下载失败: {e}")
            return False
    
    def read_file(self, remote_path: str) -> str:
        """
        读取公共文件内容
        
        Args:
            remote_path: 远程路径（相对于 /public/），如 "feclaw/index.md"
        
        Returns:
            文件内容
        """
        cos_key = self.public_prefix + remote_path.lstrip("/")
        
        try:
            content = self.storage.get_file_content(cos_key)
            if content is None:
                return f"Error: 文件不存在: /public/{remote_path}"
            
            return content.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error: 读取失败: {e}"
    
    def delete_file(self, remote_path: str) -> bool:
        """
        删除公共文件（需要确认）
        
        Args:
            remote_path: 远程路径（相对于 /public/），如 "feclaw/index.md"
        
        Returns:
            是否成功
        """
        cos_key = self.public_prefix + remote_path.lstrip("/")
        
        # 确认删除
        confirm = input(f"确认删除 /public/{remote_path}? [y/N]: ")
        if confirm.lower() != "y":
            print("已取消")
            return False
        
        try:
            self.storage.delete_file_by_key(cos_key)
            print(f"✅ 删除成功: /public/{remote_path}")
            return True
        except Exception as e:
            print(f"Error: 删除失败: {e}")
            return False
    
    def validate(self) -> dict:
        """
        验证公共文件完整性
        
        检查必需的公共文件是否存在：
        - feclaw/index.md (平台信息)
        - feclaw/principles.md (工具调用原则)
        
        Returns:
            验证结果字典
        """
        required_files = {
            "feclaw/index.md": "平台信息（FeClaw 功能说明）",
            "feclaw/principles.md": "工具调用原则",
        }
        
        results = {
            "total": len(required_files),
            "missing": [],
            "found": [],
            "status": "ok"
        }
        
        print("\n📋 验证公共文件完整性...\n")
        
        for path, desc in required_files.items():
            cos_key = self.public_prefix + path.lstrip("/")
            try:
                # 尝试获取文件元信息
                result = self.storage.file_exists(cos_key)
                if result:
                    size = result.get("size", 0)
                    results["found"].append({"path": path, "desc": desc, "size": size})
                    print(f"  ✅ {path} ({size} bytes) - {desc}")
                else:
                    raise FileNotFoundError
            except Exception:
                results["missing"].append({"path": path, "desc": desc})
                results["status"] = "incomplete"
                print(f"  ❌ {path} - {desc} [缺失]")
        
        print(f"\n结果: {len(results['found'])}/{results['total']} 文件存在")
        return results
    
    def init_default_files(self, force: bool = False) -> dict:
        """
        初始化默认公共文件
        
        Args:
            force: 是否强制覆盖已存在的文件
        
        Returns:
            操作结果字典
        """
        default_files = {
            "feclaw/index.md": """# FeClaw 智能体网关平台

## 平台概述

FeClaw 是一个智能体网关平台，为 AI Agent 提供统一的接口和服务。

## 核心功能

### 1. 虚拟文件系统 (VFS)
- 每个用户有独立的虚拟文件空间
- 支持 Linux 风格的命令操作
- 文件存储在腾讯云 COS

### 2. 智能对话
- 支持多种 LLM 模型
- 流式响应
- 工具调用能力

### 3. 记忆系统
- 每日记忆记录
- 长期记忆提炼
- 上下文压缩

## 公共空间

`/public/` 目录是只读的公共空间，包含平台信息和工具文档。

## 联系方式

如有问题，请联系平台管理员。
""",
            "feclaw/principles.md": """# 工具调用原则

## 核心原则

1. **真实调用**
   - 当需要获取信息或执行操作时，必须真正调用工具
   - 不要编造工具调用的结果
   - 不要复用过期的历史结果

2. **重新验证**
   - 历史消息中带有 [⚠️ 历史工具调用结果] 标记的内容可能已过时
   - 遇到重要信息时，请重新调用工具确认当前状态

3. **错误重试**
   - 即使历史中某个工具曾报错，也请再次尝试调用
   - 问题可能已被修复，或网络问题已恢复

## 可用工具

- `bash`: 执行 VFS 命令
- `web_search`: 联网搜索
- `spawn_subagent`: 启动子 Agent
- `edit`: 文件内容编辑（精准替换）
- `end_conversation`: 结束会话

## 注意事项

- 调用工具前请确认参数正确
- 工具调用失败时，尝试分析原因并重试
- 不要假设工具返回的内容，必须实际调用获取
"""
        }
        
        results = {
            "created": [],
            "skipped": [],
            "errors": []
        }
        
        print("\n🚀 初始化默认公共文件...\n")
        
        for path, content in default_files.items():
            cos_key = self.public_prefix + path.lstrip("/")
            
            # 检查文件是否已存在
            exists = self.storage.file_exists(cos_key) is not None
            
            if exists and not force:
                print(f"  ⏭️ {path} 已存在，跳过")
                results["skipped"].append(path)
                continue
            
            try:
                self.storage.put_object(cos_key, content.encode("utf-8"))
                action = "覆盖" if exists else "创建"
                print(f"  ✅ {path} ({action}成功)")
                results["created"].append(path)
            except Exception as e:
                print(f"  ❌ {path} 创建失败: {e}")
                results["errors"].append({"path": path, "error": str(e)})
        
        print(f"\n结果: {len(results['created'])} 创建, {len(results['skipped'])} 跳过, {len(results['errors'])} 错误")
        return results


def main() -> None:
    """主函数"""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    command = sys.argv[1]
    manager = PublicFileManager()
    
    if command == "list":
        prefix = sys.argv[2] if len(sys.argv) > 2 else ""
        files = manager.list_files(prefix)
        
        if not files:
            print(f"目录 /public/{prefix} 为空或不存在")
            return
        
        print(f"\n/public/{prefix} ({len(files)} 个文件):\n")
        print(f"{'路径':<40} {'大小':>10}")
        print("-" * 52)
        for f in files:
            size_str = f"{f['size']:,} B" if f['size'] < 1024 else f"{f['size']/1024:.1f} KB"
            print(f"{f['path']:<40} {size_str:>10}")
    
    elif command == "upload":
        if len(sys.argv) < 4:
            print("用法: python scripts/public_manager.py upload <local_path> <remote_path>")
            sys.exit(1)
        manager.upload_file(sys.argv[2], sys.argv[3])
    
    elif command == "download":
        if len(sys.argv) < 3:
            print("用法: python scripts/public_manager.py download <remote_path> [local_path]")
            sys.exit(1)
        local_path = sys.argv[3] if len(sys.argv) > 3 else None
        manager.download_file(sys.argv[2], local_path)
    
    elif command == "read":
        if len(sys.argv) < 3:
            print("用法: python scripts/public_manager.py read <remote_path>")
            sys.exit(1)
        content = manager.read_file(sys.argv[2])
        print(content)
    
    elif command == "delete":
        if len(sys.argv) < 3:
            print("用法: python scripts/public_manager.py delete <remote_path>")
            sys.exit(1)
        manager.delete_file(sys.argv[2])
    
    elif command == "validate":
        # 验证公共文件完整性
        result = manager.validate()
        if result["status"] != "ok":
            print("\n💡 提示: 使用 'init' 命令创建缺失的文件")
            sys.exit(1)
    
    elif command == "init":
        # 初始化默认公共文件
        force = "--force" in sys.argv or "-f" in sys.argv
        manager.init_default_files(force=force)
    
    else:
        print(f"未知命令: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
