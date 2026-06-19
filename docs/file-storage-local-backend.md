# 文件存储本地后端

> 版本 1 — 2026-06-19

## 1. 目标

抽象文件存储后端使 `VirtualFileSystem` 可在 `cos` 和 `local` 之间切换。调用方不关心后端。

最终效果：

```python
# 服务器部署：自动用 COS
storage = CosStorage(...)

# 桌面模式 / 开发调试：自动用本地磁盘
storage = LocalStorage(root_dir="./feclaw-storage")

# 自动选择：有 COS 配置用 COS，否则本地
storage = create_file_storage(mode="auto")
```

和已经实现的 `VectorStorage` 抽象模式完全一致。

---

## 2. 架构

### 2.1 分层

```
VirtualFileSystem (对外 API 层)
  └── self.storage: FileStorage (抽象后端)
       ├── CosStorage    (腾讯云 COS，生产部署用)
       └── LocalStorage  (本地磁盘，桌面/开发/开源用)
```

### 2.2 影响范围

| 层 | 改动 |
|---|------|
| 新增 `FileStorage` ABC | 定义 4 个纯虚方法 |
| `CosStorage` | 从 `StorageService` 提取核心方法，继承 `FileStorage` |
| 新增 `LocalStorage` | 本地磁盘实现，继承 `FileStorage` |
| `create_file_storage()` 工厂 | 根据 mode/config 选择后端 |
| `VirtualFileSystem.__init__` | 接收 `FileStorage` 实例，不再自己创建 |

---

## 3. FileStorage 接口设计

### 3.1 核心方法（5 个）

从 VFS 和外围服务的实际使用中提取。`put_object` 不返回值——29 处调用全部丢弃返回值，证明返回值是无用设计。

额外增加 `file_exists()`——`scripts/public_manager.py` 需要 `head_object` 语义（只查元数据不下载内容），`get_file_content` 会全量下载，不合理。

```python
from abc import ABC, abstractmethod
from typing import Optional, List, Dict


class FileStorage(ABC):
    """文件存储抽象基类"""

    @abstractmethod
    def get_file_content(self, key: str) -> Optional[bytes]:
        """获取文件内容
        
        Args:
            key: 存储路径
            
        Returns:
            文件字节数据，文件不存在时返回 None
        """
        ...

    @abstractmethod
    def put_object(self, key: str, file_bytes: bytes) -> None:
        """写入文件
        
        Args:
            key: 存储路径
            file_bytes: 文件字节数据
        """
        ...

    @abstractmethod
    def delete_file_by_key(self, key: str) -> bool:
        """删除文件
        
        Returns:
            True 删除成功 / False 文件不存在
        """
        ...

    @abstractmethod
    def list_objects(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        """列出前缀下的所有对象
        
        Returns:
            对象列表，每个对象含 Key, Size, LastModified 字段
            失败时返回 None
        """
        ...

    @abstractmethod
    def file_exists(self, key: str) -> Optional[Dict]:
        """检查文件是否存在并返回元数据（不下载内容）
        
        对标 COS head_object 语义。
        
        Args:
            key: 存储路径
            
        Returns:
            文件元数据 dict（含 size, mtime 等），不存在时返回 None
        """
        ...
```

### 3.2 不纳入接口的方法（COS 特有，保留在 CosStorage）

以下方法只在服务器模式需要，不加入 ABC：

| 方法 | 原因 |
|------|------|
| `generate_presigned_url/get/put` | 前端直传 COS 需要临时签名 |
| `generate_sts_credential` | COS 临时密钥，仅浏览器直传用 |
| `get_object_public_url` | COS 公开 URL，LocalStorage 用文件路径 |
| `generate_file_key` / `generate_original_file_key` | 路径生成逻辑，VFS 自己拼路径 |
| `ensure_public_file` | 公开只读文件托管 |

---

## 4. CosStorage 实现

### 4.1 改动策略

现有的 `StorageService` 类**不改名、不删除**。新增 `CosStorage` 继承 `FileStorage`，同时 `StorageService` 改为继承 `CosStorage` 保持兼容。

```
StorageService (现有类，保持兼容)
  └── CosStorage (新增，继承 FileStorage)
       └── FileStorage (ABC)
```

这样外部代码：
- `StorageService(...)` → ✅ 兼容，仍可实例化
- `FileStorage(...)` → ❌ 不行，抽象类
- `create_file_storage()` → ✅ 返回 `CosStorage` 或 `LocalStorage` 实例

```python
# services/storage_service.py

class CosStorage(FileStorage):
    """COS 文件存储实现"""

    def __init__(self):
        if not all([settings.TENCENT_COS_SECRET_ID,
                     settings.TENCENT_COS_SECRET_KEY,
                     settings.TENCENT_COS_BUCKET]):
            raise ValueError("COS 配置不完整")
        
        config = CosConfig(
            Region=settings.TENCENT_COS_REGION,
            SecretId=settings.TENCENT_COS_SECRET_ID,
            SecretKey=settings.TENCENT_COS_SECRET_KEY
        )
        self.client = CosS3Client(config)

    def get_file_content(self, key: str) -> Optional[bytes]:
        """从 COS 读取文件"""
        try:
            response = self.client.get_object(
                Bucket=settings.TENCENT_COS_BUCKET, Key=key
            )
            chunks = []
            while True:
                chunk = response['Body'].read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b''.join(chunks)
        except Exception:
            return None

    def put_object(self, key: str, file_bytes: bytes) -> None:
        """上传文件到 COS"""
        self.client.put_object(
            Bucket=settings.TENCENT_COS_BUCKET,
            Key=key,
            Body=file_bytes
        )

    def delete_file_by_key(self, key: str) -> bool:
        """从 COS 删除文件"""
        try:
            self.client.delete_object(
                Bucket=settings.TENCENT_COS_BUCKET, Key=key
            )
            return True
        except Exception:
            return False

    def list_objects(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        """列出 COS 对象"""
        try:
            all_objects = []
            marker = ""
            while True:
                response = self.client.list_objects(
                    Bucket=settings.TENCENT_COS_BUCKET,
                    Prefix=prefix,
                    MaxKeys=min(max_keys, 1000),
                    Marker=marker
                )
                contents = response.get("Contents", [])
                all_objects.extend(contents)
                if response.get("IsTruncated") == "true":
                    marker = contents[-1]["Key"]
                else:
                    break
                if len(all_objects) >= max_keys:
                    break
            return all_objects
        except Exception:
            return None

    def file_exists(self, key: str) -> Optional[Dict]:
        """检查 COS 文件是否存在（head_object）"""
        try:
            result = self.client.head_object(
                Bucket=settings.TENCENT_COS_BUCKET, Key=key
            )
            return {
                "exists": True,
                "size": result.get("ContentLength", 0),
                "content_type": result.get("ContentType", ""),
            }
        except Exception:
            return None

    # --- COS 特有方法（不纳入 ABC） ---

    def generate_presigned_url(self, user_id: int, filename: str) -> Tuple[str, str]:
        """生成预签名上传 URL"""
        ...

    def generate_sts_credential(self, user_id: str, ...) -> dict:
        """生成临时密钥"""
        ...

    def generate_presigned_get_url(self, key: str, expired: int = 3600*7*24) -> str:
        """生成预签名下载 URL"""
        ...

    def generate_presigned_put_url(self, key: str, expired: int = 3600) -> str:
        """生成预签名上传 URL"""
        ...

    def get_object_public_url(self, key: str) -> str:
        """获取 COS 公开 URL"""
        ...

    def ensure_public_file(self, rel_path: str, content: str) -> bool:
        """确保公开文件存在"""
        ...

    def generate_original_file_key(self, user_id: int, filename: str) -> str:
        """生成原图文键"""
        ...

    def generate_cleaned_file_key(self, user_id: int, file_sha1: str) -> str:
        """生成去手写图键"""
        ...

    def generate_file_key(self, user_id: int, filename: str) -> str:
        """生成文件键"""
        ...


class StorageService(CosStorage):
    """兼容旧代码——直接继承 CosStorage，一切接口不变
    
    所有调用 `StorageService(...)` 的旧代码无需改动。
    新增代码应使用 `create_file_storage()` 或直接 `CosStorage()`。
    """
    pass
```

### 4.2 `put_object` 返回值清理

现有 `StorageService.put_object()` 返回 `str`（URL），但如上所述所有 29 处调用都丢弃返回值。

**改动**：在新 `CosStorage.put_object()` 中改为 `-> None`。`StorageService` 兼容类可保留旧签名或同步修改。

---

## 5. LocalStorage 实现

### 5.1 路径映射规则

```
COS key:    feclaw/user_1/original/abc.jpg
本地路径:   ./feclaw-storage/feclaw/user_1/original/abc.jpg

COS key:    feclaw/public/index.html
本地路径:   ./feclaw-storage/feclaw/public/index.html
```

VFS 传入的路径以 `feclaw/` 开头（`settings.TENCENT_COS_PREFIX`）。LocalStorage 不做特殊处理，直接映射。

### 5.2 安全设计：路径穿越防护

```python
def _resolve(self, key: str) -> str:
    """将 COS key 映射为安全本地路径"""
    # 1. 统一分隔符
    safe = key.lstrip("/").replace("\\", "/")
    # 2. 规范化路径（解析 ..）
    path = os.path.normpath(os.path.join(self.root, safe))
    # 3. 防路径穿越
    if not path.startswith(os.path.normpath(self.root)):
        raise SecurityError(f"Path traversal detected: {key}")
    return path
```

### 5.3 完整实现

```python
# services/local_storage.py

import os
import logging
from datetime import datetime
from typing import Optional, List, Dict

from services.file_storage import FileStorage

logger = logging.getLogger(__name__)


class LocalStorage(FileStorage):
    """本地文件系统存储实现"""

    def __init__(self, root_dir: str = "./feclaw-storage"):
        self.root = os.path.abspath(root_dir)
        os.makedirs(self.root, exist_ok=True)
        logger.info(f"[LocalStorage] root={self.root}")

    def _resolve(self, key: str) -> str:
        """安全解析 key 到本地路径"""
        safe = key.lstrip("/").replace("\\", "/")
        path = os.path.normpath(os.path.join(self.root, safe))
        if not path.startswith(os.path.normpath(self.root)):
            raise ValueError(f"Path traversal: {key}")
        return path

    def get_file_content(self, key: str) -> Optional[bytes]:
        path = self._resolve(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception as e:
            logger.error(f"[LocalStorage] read failed: {key}, {e}")
            return None

    def put_object(self, key: str, file_bytes: bytes) -> None:
        path = self._resolve(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(file_bytes)

    def delete_file_by_key(self, key: str) -> bool:
        path = self._resolve(key)
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
            return True
        except Exception as e:
            logger.error(f"[LocalStorage] delete failed: {key}, {e}")
            return False

    def list_objects(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        dir_path = self._resolve(prefix)
        if not os.path.isdir(dir_path):
            return []
        results = []
        try:
            for root, dirs, files in os.walk(dir_path):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, self.root).replace("\\", "/")
                    stat = os.stat(full)
                    results.append({
                        "Key": rel,
                        "Size": stat.st_size,
                        "LastModified": datetime.fromtimestamp(stat.st_mtime),
                    })
                    if len(results) >= max_keys:
                        return results
        except Exception as e:
            logger.error(f"[LocalStorage] list failed: {prefix}, {e}")
            return None
        return results

    def file_exists(self, key: str) -> Optional[Dict]:
        """检查文件是否存在并返回元数据"""
        path = self._resolve(key)
        if not os.path.exists(path):
            return None
        try:
            stat = os.stat(path)
            return {
                "exists": True,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "is_dir": os.path.isdir(path),
            }
        except Exception as e:
            logger.error(f"[LocalStorage] stat failed: {key}, {e}")
            return None
```

---

## 6. 工厂函数

```python
# services/file_storage.py

from abc import ABC, abstractmethod
from typing import Optional, List, Dict

import logging
from config import settings

logger = logging.getLogger(__name__)


class FileStorage(ABC):
    """文件存储抽象基类——4 个核心方法"""
    
    @abstractmethod
    def get_file_content(self, key: str) -> Optional[bytes]:
        ...

    @abstractmethod
    def put_object(self, key: str, file_bytes: bytes) -> None:
        ...

    @abstractmethod
    def delete_file_by_key(self, key: str) -> bool:
        ...

    @abstractmethod
    def list_objects(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        ...

    @abstractmethod
    def file_exists(self, key: str) -> Optional[Dict]:
        ...


def create_file_storage(mode: str = "auto") -> FileStorage:
    """自动选择存储后端
    
    Args:
        mode: "auto" | "cos" | "local"
            auto: 有 COS 配置则用 COS，否则本地
            cos:  强制 COS（COS 配置不完整时抛异常）
            local: 强制本地磁盘
    """
    if mode == "local":
        from services.local_storage import LocalStorage
        root = getattr(settings, "LOCAL_STORAGE_ROOT", "./feclaw-storage")
        return LocalStorage(root_dir=root)
    
    cos_configured = all([
        settings.TENCENT_COS_SECRET_ID,
        settings.TENCENT_COS_SECRET_KEY,
        settings.TENCENT_COS_BUCKET,
    ])
    
    if mode == "cos" and not cos_configured:
        raise ValueError("COS mode requires TENCENT_COS_* config")
    
    if cos_configured:
        from services.storage_service import CosStorage
        return CosStorage()
    
    if mode == "auto":
        logger.info("COS not configured, falling back to LocalStorage")
        from services.local_storage import LocalStorage
        root = getattr(settings, "LOCAL_STORAGE_ROOT", "./feclaw-storage")
        return LocalStorage(root_dir=root)
    
    raise ValueError(f"Unknown storage mode: {mode}")
```

---

## 7. Config 新增

```python
# config.py 新增字段

# 存储后端
STORAGE_MODE: str = "auto"               # "auto" | "cos" | "local"
LOCAL_STORAGE_ROOT: str = "./feclaw-storage"
```

---

## 8. VirtualFileSystem 接入

### 8.1 构造方法改动

```python
# services/virtual_filesystem.py

class VirtualFileSystem:
    def __init__(self, storage: Optional[FileStorage] = None):
        if storage is not None:
            self.storage = storage
        else:
            self.storage = create_file_storage(mode=settings.STORAGE_MODE)
```

调用方传入 `storage` 时使用传入的实例（用于测试或特殊场景）。不传时自动选择。

### 8.2 使用 `put_object` 返回值的地方

所有调用 `self.storage.put_object(...)` 的 29 处都不需要改动——因为本来就没有接收返回值。

---

## 9. main.py 接入

```python
# main.py lifespan 改动

from services.file_storage import create_file_storage

async def lifespan(app):
    # 创建文件存储（根据模式自动选择）
    storage = create_file_storage(mode=settings.STORAGE_MODE)
    app.state.storage = storage
    
    # VFS 使用指定的 storage
    vfs = VirtualFileSystem(storage=storage)
    
    # FUSE 初始化只在 COS 模式下有意义
    if settings.FUSE_ENABLED and isinstance(storage, CosStorage):
        # 原有 FUSE 启动逻辑...
        pass
```

---

## 10. 一致性对照

### 与 VectorStorage 的模式对比

| 维度 | VectorStorage | FileStorage |
|------|-------------|-------------|
| ABC 文件 | `services/vector_storage/__init__.py` | `services/file_storage.py` |
| COS 实现 | `CosVectorStorage` | `CosStorage` |
| 本地实现 | `SqliteVecStorage` | `LocalStorage` |
| 工厂函数 | `VectorSearchService._create_storage()` | `create_file_storage()` |
| 选择逻辑 | `settings.VECTOR_STORAGE_BACKEND` | `settings.STORAGE_MODE` |
| 调用方式 | `vs = VectorSearchService()` | `vfs = VirtualFileSystem()` |

### 与现行 StorageService 的兼容性

```
旧代码: storage = StorageService()        → ✅ 不变，StorageService 继承 CosStorage
旧代码: storage.put_object(key, bytes)    → ✅ 返回 str → 改为 None，但 29 处调用全丢弃
旧代码: storage.get_file_content(key)    → ✅ 签名不变
新代码: storage = create_file_storage()  → ✅ 新增
新代码: vfs = VirtualFileSystem()         → ✅ 自动用工厂创建
```

---

## 11. 改动的文件清单

| 文件 | 动作 | 说明 |
|------|------|------|
| `services/file_storage.py` | **新建** | `FileStorage` ABC + `create_file_storage()` 工厂 |
| `services/local_storage.py` | **新建** | `LocalStorage` 实现 |
| `services/storage_service.py` | **重构** | 抽 `CosStorage` 类，`StorageService` 改为继承它 |
| `services/virtual_filesystem.py` | **修改** | `__init__` 接受可选 `storage` 参数 |
| `config.py` | **修改** | 加 `STORAGE_MODE` 和 `LOCAL_STORAGE_ROOT` |
| `main.py` | **修改** | `lifespan` 中用 `create_file_storage()` |
| `services/vfs/cos_client.py` | **修改** | `list_objects_raw()` 改用标准 `list_objects()` |
| `services/agent_cleanup_service.py` | **修改** | `client.delete_object()` → `delete_file_by_key()` |
| `routers/sandbox.py` | **修改** | 同上 |
| `scripts/public_manager.py` | **修改** | 4 处 COS 原生调用改用抽象接口 |
| `docs/file-storage-local-backend.md` | **本文** | 设计文档 |

---

## 12. 验证清单

- [ ] `CosStorage.get_file_content()` 返回 bytes 或 None
- [ ] `CosStorage.put_object()` 写入后文件存在，不返回值
- [ ] `CosStorage.delete_file_by_key()` 删除成功返回 True，不存在返回 False
- [ ] `CosStorage.list_objects()` 返回正确格式
- [ ] `LocalStorage.get_file_content()` 返回 bytes 或 None
- [ ] `LocalStorage.put_object()` 写入后磁盘文件存在
- [ ] `LocalStorage._resolve()` 拒绝路径穿越（`../../etc/passwd`）
- [ ] `LocalStorage.list_objects()` 返回与 COS 相同格式
- [ ] `create_file_storage("auto")` 有 COS 配置时选 COS，无时选本地
- [ ] `create_file_storage("local")` 强制本地
- [ ] `StorageService(...)` 旧代码兼容
- [ ] `VirtualFileSystem(storage=xxx)` 正常创建
- [ ] `VirtualFileSystem()` 自动创建
- [ ] 所有 `self.storage.client.*` 直接调用已替换为抽象接口
- [ ] `public_manager.py` 的 `head_object` 改为 `storage.file_exists()`

---

## 13. 参考

审计报告：[file-storage-audit-report.md](./file-storage-audit-report.md)
同模式现有实现：[vector-storage-local-backend.md](./vector-storage-local-backend.md)
