# FileStorage 抽象层设计审阅报告

> 审阅日期：2026-06-19
> 审阅对象：docs/file-storage-local-backend.md
> 审阅者：Claude (Architecture Reviewer)

---

## 总体评价

设计思路正确：与已有的 VectorStorage 模式一致，4 个核心方法的提取合理，`put_object` 返回值清理有数据支撑。但发现 **2 个 P0 阻塞项**和 **3 个 P1 重要问题**，主要是 **VFS 层和外部调用方中存在直接访问 `self.storage.client`（COS SDK 原生对象）的代码**，这些代码在切换到 `LocalStorage` 时会因 `AttributeError`（`LocalStorage` 无 `.client` 属性）而崩溃。

## 评分汇总

| 级别 | 数量 | 说明 |
|------|------|------|
| 🔴 P0 | 2 | 必须修，否则 LocalStorage 模式会运行时崩溃 |
| 🟡 P1 | 3 | 建议修，存在设计遗漏或架构不完整 |
| 🟢 P2 | 3 | 可修可不修，不影响核心功能 |
| ✅ 通过 | — | 其余设计方面合理 |

---

## 🔴 P0 — 必须修复

### P0-1: `CosClient.list_objects_raw()` 直接访问 `self.storage.client`（COS SDK 原生对象）

- **文件**: `services/vfs/cos_client.py:65-78`
- **问题**: `list_objects_raw()` 方法调用 `self.storage.client.list_objects(Bucket=..., Prefix=..., MaxKeys=...)`。`LocalStorage` 没有 `.client` 属性（COS SDK 原生对象），会导致 `AttributeError`。
- **影响链路**:
  ```
  VFS._get_public_files()  →  self.cos_client.list_objects_raw(...)
    →  self.storage.client.list_objects(...)   ← 💥 LocalStorage 无 .client
  ```
- **修复建议**:
  - 方案 A（推荐）：在 `FileStorage` ABC 中增加 `list_objects_raw(bucket, prefix, max_keys)` 方法，或直接用现有的 `list_objects(prefix, max_keys)` 替代（注意 `list_objects_raw` 多了一个 `bucket` 参数 — 在 LocalStorage 实现中可直接忽略）。
  - 方案 B：重构 `_get_public_files()` 使用标准的 `self.storage.list_objects(cos_prefix, max_keys=1000)`，去掉 `bucket` 参数依赖。

### P0-2: 3 处代码绕过抽象层直接调用 `self.storage.client.*`（COS SDK 原生方法）

- **文件与行号**:
  1. `services/agent_cleanup_service.py:233-236` — `self.storage.client.delete_object(Bucket=..., Key=key)`
  2. `routers/sandbox.py:263-265` — `manager.vfs.storage.client.delete_object(Bucket=..., Key=cos_key)`
  3. `scripts/public_manager.py:54,179,218,321` — `self.storage.client.list_objects/delete_object/head_object`
- **问题**: 这些调用直接访问 COS SDK 的 `client` 对象，`LocalStorage` 无此属性，运行时报 `AttributeError`。
- **修复建议**:
  - `agent_cleanup_service.py:233` — 改用 `self.storage.delete_file_by_key(key)`（该方法已在 ABC 中定义）
  - `routers/sandbox.py:263` — 改用 `manager.vfs.storage.delete_file_by_key(cos_key)`
  - `scripts/public_manager.py` — 改用抽象接口 `get_file_content` / `put_object` / `delete_file_by_key`；如果确实需要 `head_object`（检查文件是否存在），可在 ABC 中增加 `file_exists(key) -> bool` 方法

---

## 🟡 P1 — 建议修复

### P1-1: `chat_service.py._read_vfs_files_async()` 完全绕过 FileStorage 抽象层

- **文件**: `services/chat_service.py:556-586`
- **问题**: 该方法硬编码了 COS URL 构建和 presigned URL 逻辑：
  ```python
  # 直接拼 COS URL
  cos_url = f"https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{cos_key}"
  # 调用 COS 特有方法
  presigned = storage_svc.generate_presigned_get_url(cos_url, expired=3600)
  ```
  这完全不可移植到 LocalStorage 模式。虽然设计意图是通过 httpx 并行下载加速，但这种方式绑死了 COS。
- **修复建议**:
  - 在 `FileStorage` ABC 中增加 `get_file_content_async(key)` 或在 `LocalStorage` 中实现本地的异步读取
  - 或者在 `_read_vfs_files_async()` 中检测后端类型：COS 用 presigned URL 加速，Local 用直接的 `storage.get_file_content()` + `asyncio.to_thread()`
  - 更简单方案：`_read_vfs_files_async` 直接调用 `self.vfs.cat(path)`（在 thread pool 中执行），让 VFS 内部处理后端差异

### P1-2: CosStorage 缺少 async 包装方法

- **文件**: 设计文档 `docs/file-storage-local-backend.md` §4
- **问题**: 现有 `StorageService` 有 8 个 `*_async()` 方法（`upload_file_async`, `put_object_async`, `get_file_content_async`, `list_objects_async`, `delete_file_by_key_async`, `delete_file_async`, `ensure_public_file_async`, `generate_presigned_url_async`），它们是对同步方法的 `asyncio.to_thread()` 包装。设计文档的 `CosStorage` 未提及这些方法，`LocalStorage` 也未提供。
- **影响**: VFS 的某些调用链和外围服务可能依赖这些 async 方法。如果 `StorageService(CosStorage)` 继承模式保留这些方法在 `CosStorage` 上，`LocalStorage` 也需要对应的 async 包装。
- **修复建议**: 在 `FileStorage` ABC 或 mixin 中提供通用的 `asyncio.to_thread()` async 包装，子类自动继承。或者明确在文档中列出所有 async 方法的来源和去向。

### P1-3: VFS._get_public_base_path() 语义上绑定 COS 配置

- **文件**: `services/virtual_filesystem.py:1006-1008`
- **问题**: `_get_public_base_path()` 返回 `f"{settings.TENCENT_COS_PREFIX}public/"`。虽然 `TENCENT_COS_PREFIX` 的值（`"feclaw/"`）在 LocalStorage 模式下作为路径前缀也可正常工作，但配置名和语义是 COS 专有的。
- **修复建议**: 在 `config.py` 中新增 `STORAGE_KEY_PREFIX`（默认值等于 `TENCENT_COS_PREFIX`），VFS 使用新配置名。旧 `TENCENT_COS_PREFIX` 作为 fallback。或至少在文档中明确说明 LocalStorage 也依赖 `TENCENT_COS_PREFIX` 作为路径前缀。

---

## 🟢 P2 — 可修可不修

### P2-1: SecurityError vs ValueError 不一致

- **文件**: 设计文档 §5.2 第 28 行 vs §5.3 第 26 行
- **问题**: 安全设计章节用 `SecurityError`，完整实现中用 `ValueError`。实现中一致用 `ValueError` 即可，建议统一为后者。

### P2-2: `StorageService.delete_file(url)` 不被 ABC 覆盖

- **文件**: `services/storage_service.py:245-269`
- **问题**: 该方法按 URL（而非 key）删除文件，是 COS 特有 API。设计文档正确地将它排除出 ABC。无实际影响，但需注意如果有外部代码调用此方法，迁移到 LocalStorage 时会失败。

### P2-3: LocalStorage.list_objects() 中的日期对象 vs COS 的字符串格式差异

- **文件**: 设计文档 §5.3 第 61-64 行
- **问题**: COS 的 `list_objects` 返回的 `LastModified` 是字符串，而 `LocalStorage` 返回的是 Python `datetime` 对象。VFS 中的 `FileEntry._parse_cos_date()` 期望解析日期字符串。如果消费者代码直接读取 `LastModified` 字段做类型判断，可能出问题。
- **修复建议**: 统一返回格式 — 要么都是 ISO 8601 字符串，要么都用文档约定返回值。

---

## 逐项审阅

### 1. 接口设计（4 方法是否足够）

**结论: 基本足够，但有 1 个遗漏** ✅

4 个核心方法覆盖了 VFS 的主要操作（读、写、删、列）。但 `CosClient.list_objects_raw(bucket, prefix, max_keys)` 是 `list_objects` 的变体，多了 `bucket` 参数 — 这个已有标准 `list_objects` 可替代，不需要新增。`head_object`（文件存在性检查）在 `public_manager.py` 中使用，可在 ABC 中增加 `file_exists(key) -> bool`。

### 2. LocalStorage 安全性

**结论: 基本完善** ✅

- 路径穿越防护：`_resolve()` 的三步设计（strip、normpath、prefix check）是正确的
- 建议：增加 `os.path.realpath()` 替代 `os.path.normpath()` 以解析符号链接，防止符号链接穿越沙箱

### 3. CosStorage 改造方案

**结论: 合理** ✅

`StorageService` 继承 `CosStorage` 而非直接改名的策略，保持了所有 `StorageService()` 直接调用的向后兼容性。`put_object` 返回值清理有充分的数据支撑。

### 4. VFS 接入方式

**结论: 有遗漏** 🟡

设计文档 §8 的 VFS 接入只考虑了构造方法改动，未涉及：
- `CosClient` 类（services/vfs/cos_client.py）中 `list_objects_raw` 的 COS 依赖
- `sandbox.py`、`agent_cleanup_service.py` 等绕过 VFS.storage 直接访问 `.client` 的调用方
- `chat_service.py._read_vfs_files_async()` 的 COS 硬编码

### 5. 与 VectorStorage 模式的一致性

**结论: 一致** ✅

| 对比维度 | VectorStorage | FileStorage | 一致性 |
|---------|-------------|------------|--------|
| ABC 定义 | ✅ `services/vector_search_service.py:103` | ✅ `services/file_storage.py` | 一致 |
| 工厂创建 | `_create_storage()` 私有方法 | `create_file_storage()` 模块级函数 | 细微差异 |
| 配置键 | `VECTOR_STORAGE_BACKEND` | `STORAGE_MODE` | 一致 |
| COS 实现 | `CosVectorStorage` | `CosStorage` | 一致 |
| 本地实现 | `SqliteVecStorage` / `NumpyVecStorage` | `LocalStorage` | 一致 |

### 6. 遗漏的调用方或依赖关系

**结论: 有遗漏，详见 P0 项** 🔴

设计文档声称影响范围只涉及 5 个文件，但以下文件和调用也被影响：

| 文件 | 操作 | 影响 |
|------|------|------|
| `services/vfs/cos_client.py:65-78` | `self.storage.client.list_objects()` | P0 — 直接 COS 调用 |
| `services/agent_cleanup_service.py:233` | `self.storage.client.delete_object()` | P0 — 直接 COS 调用 |
| `routers/sandbox.py:263` | `vfs.storage.client.delete_object()` | P0 — 直接 COS 调用 |
| `scripts/public_manager.py:54,179,218,321` | `.client.list_objects/delete_object/head_object` | P0 — 直接 COS 调用 |
| `services/chat_service.py:556-586` | 硬编码 COS URL + presigned | P1 — 绕过抽象 |

---

## 最终结论

**有条件通过** 🔴→🟡

P0 问题（直接 COS client 访问）必须在合入前修复，否则 LocalStorage 模式会在运行时崩溃。P1 问题建议在本迭代中修复。P2 可在后续迭代处理。

### 修复优先级

1. **立即修** (P0): 将所有 `self.storage.client.*` 调用替换为抽象接口方法
2. **本迭代** (P1): 修复 `_read_vfs_files_async()` 的 COS 硬编码；增加 async 包装；统一路径前缀配置
3. **后续** (P2): `SecurityError` 统一；`LastModified` 格式统一；文档更新
