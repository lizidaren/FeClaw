# 向量存储本地后端：sqlite-vec 方案（拆表版）

> 版本 3 — 2026-06-18，拆表策略 + code review v1 反馈全部修复

## 1. 目标

抽象存储后端使 `VectorSearchService` 可在 `cos` 和 `sqlite` 之间切换。**对外 API 完全一致**，调用方不关心后端。

调用体验：

```python
vs = VectorSearchService(agent_hash="abc123")
vs.ensure_index("idx-abc123-kb")
vs.index_batch(items, index="idx-abc123-kb")
results = await vs.search(query, index="idx-abc123-kb", top_k=5)
await vs.delete(keys, index="idx-abc123-kb")
```

和 Pinecone / Qdrant / COS Vector 一致。

---

## 2. 架构

### 2.1 分层

```
VectorSearchService (对外 API 层)
  └── self.storage: VectorStorage (抽象后端)
       ├── CosVectorStorage    (腾讯云 COS，生产用)
       └── SqliteVecStorage    (本地 SQLite, 开发/开源部署用)
```

### 2.2 VectorStorage 抽象

```python
class VectorStorage(ABC):
    def ensure_index(self, index: str) -> None
    def query(self, index: str, query_vec, top_k, filter=None) -> List[Dict]
    def put(self, index: str, vectors: List[Dict]) -> None
    def delete(self, index: str, keys: List[str]) -> None
    def list_keys_by_prefix(self, index: str, prefix: str) -> List[str]
```

### 2.3 VectorSearchService 改动

```python
class VectorSearchService:
    def __init__(self, agent_hash=None):
        self.agent_hash = agent_hash
        self.storage = self._create_storage()
    
    def _create_storage(self) -> VectorStorage:
        backend = settings.VECTOR_STORAGE_BACKEND or "cos"
        if backend == "sqlite":
            return SqliteVecStorage()
        return CosVectorStorage(agent_hash=self.agent_hash)
    
    # 所有现有公开方法签名不变，内部改为 self.storage.*
    def _query_index(self, vec, index, top_k, filter=None, timeout=15.0):
        return await asyncio.to_thread(self.storage.query, ...)
    
    async def index_batch(self, items, index):
        # embedding 逻辑不变 ...
        await asyncio.to_thread(self.storage.put, index, cos_vectors)
    
    async def delete(self, keys, index):
        await asyncio.to_thread(self.storage.delete, index, keys)
```

---

## 3. SQLite 设计（拆表策略）

### 3.1 核心决策：每 index 一张 vec0 表

```
不再用"一张 vec0 表存所有 index + JOIN 过滤"，
改为"每 index 独立一张 vec0 表"。

理由：
- MATCH 在属于该 index 的精确数据集上运行，无需 post-filter
- 没有"大海捞针"问题（小 index 搜索被大 index 淹没）
- 查询语义最直白：搜什么 index→查什么表
- 都在同一个 .db 文件中，备份管理不变
```

### 3.2 表名映射

```python
import re

# index 名 → 安全的 vec0 表名
INDEX_TABLE_PREFIX = "v0_"

# 输入校验：只允许字母数字和连字符
_INDEX_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+$')

def _validate_index(index: str):
    """校验 index 名：长度 ≤ 100，只含字母数字_-"""
    if not index or len(index) > 100:
        raise ValueError(f"Invalid index name: {index!r} (empty or too long, max 100)")
    if not _INDEX_NAME_RE.match(index):
        raise ValueError(f"Invalid index name: {index!r} (only a-zA-Z0-9_- allowed)")

def _index_to_table(index: str) -> str:
    """idx-abc123-kb → v0_idx_abc123_kb
    
    安全策略：
    1. 输入校验拒绝非法字符
    2. 替换 - . 为 _
    3. SQLite 标识符用 [] 引用 + ]] 转义
    """
    _validate_index(index)
    safe = index.replace('-', '_').replace('.', '_')
    return f"{INDEX_TABLE_PREFIX}{safe}"

def _escape_table(table: str) -> str:
    """SQLite 表名转义：]] → ]]（双写右方括号）"""
    return table.replace(']', ']]')

def _get_table_by_index(conn, index: str) -> str:
    """从 vec_indexes 表查 table_name，不依赖字符串逆向映射"""
    cur = conn.execute(
        "SELECT table_name FROM vec_indexes WHERE index_name = ?", (index,)
    )
    row = cur.fetchone()
    if row:
        return row[0]
    raise KeyError(f"Index {index!r} not registered")
```

### 3.3 Schema

```sql
-- 索引注册表：必须先创建，vec_entries 外键依赖
CREATE TABLE IF NOT EXISTS vec_indexes (
    index_name TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,               -- 对应的 vec0 表名
    created_at TEXT DEFAULT (datetime('now')),
    vector_count INTEGER DEFAULT 0          -- 由触发器自动维护
);

-- 元数据统一表：存所有 index 的 vec_key、metadata
-- ensure_index() 先注册 vec_indexes，然后才能插入数据
CREATE TABLE IF NOT EXISTS vec_entries (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    index_name TEXT NOT NULL
        REFERENCES vec_indexes(index_name) ON DELETE CASCADE,
    vec_key TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',            -- JSON 字符串
    created_at TEXT DEFAULT (datetime('now')),
    
    -- 同 index 内 vec_key 唯一
    UNIQUE(index_name, vec_key)
);
CREATE INDEX IF NOT EXISTS idx_entries_index ON vec_entries(index_name);


-- 触发器：插入→递增 vec_indexes.vector_count
CREATE TRIGGER IF NOT EXISTS tr_entries_insert 
AFTER INSERT ON vec_entries
BEGIN
    UPDATE vec_indexes SET vector_count = vector_count + 1 
    WHERE index_name = NEW.index_name;
END;

-- 触发器：删除→递减 vec_indexes.vector_count
CREATE TRIGGER IF NOT EXISTS tr_entries_delete
AFTER DELETE ON vec_entries
BEGIN
    UPDATE vec_indexes SET vector_count = vector_count - 1 
    WHERE index_name = OLD.index_name;
END;

-- 注意：vec0 表是动态创建的，不写在初始 schema 中
-- CREATE VIRTUAL TABLE v0_xxx USING vec0(embedding float[1024] distance_metric=cosine);
-- 在 ensure_index() 中按需创建
```

### 3.4 连接管理

```python
_sqlite_connection: sqlite3.Connection | None = None
_sqlite_lock = threading.Lock()

def _get_sqlite_conn() -> sqlite3.Connection:
    """模块级单例，避免每个 VectorSearchService 开新连接"""
    global _sqlite_connection
    if _sqlite_connection is not None:
        return _sqlite_connection
    
    db_path = os.path.join(
        settings.DATA_DIR or os.path.join(os.path.dirname(__file__), '..', 'data'),
        "vectors.db"
    )
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    
    _init_schema(conn)
    _sqlite_connection = conn
    return conn
```

---

## 4. 核心操作实现

### 4.1 ensure_index

```python
def ensure_index(self, index: str):
    """创建 index：vec0 表 + 元数据注册。幂等，单事务。"""
    table = _index_to_table(index)
    escaped_table = _escape_table(table)
    
    with self._conn:
        # 检查是否已注册
        cur = self._conn.execute(
            "SELECT 1 FROM vec_indexes WHERE index_name = ?", (index,)
        )
        if cur.fetchone():
            return
        
        # 创建 vec0 表。CREATE VIRTUAL TABLE 不支持 IF NOT EXISTS
        # 在事务外并发创建时可能报 "already exists"，吞这个特定错误
        try:
            self._conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS [{escaped_table}] 
                USING vec0(embedding float[1024] distance_metric=cosine)
            """)
        except sqlite3.OperationalError as e:
            err = str(e).lower()
            if "already exists" in err:
                pass  # 并发安全：另一线程刚建好
            else:
                logger.error("Failed to create vec0 table %s: %s", table, e)
                raise
        
        # 注册到 vec_indexes（幂等）
        self._conn.execute(
            "INSERT OR IGNORE INTO vec_indexes (index_name, table_name) VALUES (?, ?)",
            (index, table)
        )
```

### 4.2 put

```python
def put(self, index: str, vectors: List[Dict]):
    """
    vectors: [{key, data: {float32: [...]}, metadata: {...}}]
    每个向量：unified metadata + dedicated vec0 entry，同一事务。
    """
    table = _index_to_table(index)
    escaped = _escape_table(table)
    
    with self._conn:
        # ensure_index 会注册 vec_indexes 并创建 vec0 表
        # FK 约束确保 put 必须在 ensure_index 之后
        self.ensure_index(index)
        
        for v in vectors:
            vec_bytes = struct.pack(f'{1024}f', *v["data"]["float32"])
            
            # 处理重复 key：先删旧的
            cur = self._conn.execute(
                "SELECT rowid FROM vec_entries WHERE index_name = ? AND vec_key = ?",
                (index, v["key"])
            )
            existing = cur.fetchone()
            if existing:
                self._conn.execute(
                    f"DELETE FROM [{escaped}] WHERE rowid = ?", (existing[0],)
                )
                self._conn.execute(
                    "DELETE FROM vec_entries WHERE rowid = ?", (existing[0],)
                )
            
            # 插入新元数据（触发器自动更新 vec_indexes.vector_count）
            cur = self._conn.execute(
                "INSERT INTO vec_entries (index_name, vec_key, metadata) VALUES (?, ?, ?)",
                (index, v["key"], json.dumps(v.get("metadata", {}), ensure_ascii=False))
            )
            new_id = cur.lastrowid
            
            # 插入向量到专用的 vec0 表
            self._conn.execute(
                f"INSERT INTO [{escaped}] (rowid, embedding) VALUES (?, ?)",
                (new_id, vec_bytes)
            )
```

### 4.3 delete

```python
def delete(self, index: str, keys: List[str]):
    table = _index_to_table(index)
    escaped = _escape_table(table)
    
    with self._conn:
        for key in keys:
            cur = self._conn.execute(
                "SELECT rowid FROM vec_entries WHERE index_name = ? AND vec_key = ?",
                (index, key)
            )
            row = cur.fetchone()
            if not row:
                continue
            entry_id = row[0]
            
            # 删向量
            self._conn.execute(f"DELETE FROM [{escaped}] WHERE rowid = ?", (entry_id,))
            # 删元数据（触发器自动递减 vector_count）
            self._conn.execute("DELETE FROM vec_entries WHERE rowid = ?", (entry_id,))
```

### 4.4 query

```python
def query(self, index: str, query_vec: List[float], top_k: int,
          filter: dict = None) -> List[Dict]:
    """
    拆表搜索：直接在对应 index 的 vec0 表上 MATCH。
    无需后过滤，因为表里全是属于该 index 的向量。
    
    先查 vec_indexes 确认 index 已注册，未注册则返回空。
    """
    try:
        table = _get_table_by_index(self._conn, index)
    except KeyError:
        logger.debug("query on unregistered index %s, returning []", index)
        return []
    
    escaped = _escape_table(table)
    query_bytes = struct.pack(f'{1024}f', *query_vec)
    
    # 用 top_k * 3 取候选，filter 过滤后确保至少满 top_k
    fetch_k = top_k * 3
    
    try:
        cur = self._conn.execute(
            f"SELECT v.rowid, v.distance FROM [{escaped}] v "
            f"WHERE v.embedding MATCH ? AND k = ?",
            (query_bytes, fetch_k)
        )
        candidates = cur.fetchall()
    except Exception as e:
        logger.warning("MATCH on %s failed: %s", table, e)
        return []
    
    if not candidates:
        return []
    
    # 从 vec_entries 获取元数据
    rowids = [r[0] for r in candidates]
    placeholders = ','.join('?' * len(rowids))
    
    cur = self._conn.execute(
        f"SELECT rowid, vec_key, metadata FROM vec_entries "
        f"WHERE rowid IN ({placeholders})",
        rowids
    )
    meta_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    
    results = []
    for rowid, distance in candidates:
        if rowid not in meta_map:
            continue
        vec_key, metadata_str = meta_map[rowid]
        
        # 应用 filter
        if filter:
            metadata = json.loads(metadata_str) if metadata_str else {}
            if not self._match_filter(metadata, filter):
                continue
        
        score = max(0.0, min(1.0, 1.0 - distance))
        results.append({
            "key": vec_key,
            "score": score,
            "metadata": json.loads(metadata_str) if metadata_str else {},
        })
    
    return results

def _match_filter(self, metadata: dict, filter: dict) -> bool:
    """Python 侧 metadata 过滤"""
    for field, condition in filter.items():
        if isinstance(condition, dict):
            if "$in" in condition:
                if metadata.get(field) not in condition["$in"]:
                    return False
        else:
            if metadata.get(field) != condition:
                return False
    return True
```

### 4.5 list_keys_by_prefix

```python
def list_keys_by_prefix(self, index: str, prefix: str) -> List[str]:
    escaped = prefix.replace('%', '\\%').replace('_', '\\_')
    cur = self._conn.execute(
        "SELECT vec_key FROM vec_entries "
        "WHERE index_name = ? AND vec_key LIKE ? ESCAPE '\\'",
        (index, escaped + '%')
    )
    return [row[0] for row in cur.fetchall()]
```

---

## 5. 配置项

```python
# config.py 新增

# 向量存储后端: "cos"（腾讯云）或 "sqlite"（本地）
VECTOR_STORAGE_BACKEND: str = "cos"

# SQLite 后端专用
VECTOR_SQLITE_PATH: str = "data/vectors.db"
```

### 5.1 路径统一

`VECTOR_SQLITE_PATH` 应以 `settings.DATA_DIR` 为前缀：

```python
# _get_sqlite_conn 中：
db_path = os.path.join(
    settings.DATA_DIR or os.path.join(os.path.dirname(__file__), '..', 'data'),
    os.path.basename(settings.VECTOR_SQLITE_PATH)
)
```

这样 `VECTOR_SQLITE_PATH` 可配完整的子路径，根目录默认在 `DATA_DIR`。

---

## 6. VectorSearchService 新增公共方法

为确保 vfs_indexer 不直接访问 `.storage`，`VectorSearchService` 增加一个 public 委托方法：

```python
class VectorSearchService:
    # ...
    
    def list_keys_by_prefix(self, index: str, prefix: str) -> List[str]:
        """暴露给 vfs_indexer 等外部调用方，不直接访问 self.storage"""
        return self.storage.list_keys_by_prefix(index, prefix)
```

`vfs_indexer.py` 的 `_delete_file_vectors` 改为调用此方法：

```python
# 旧代码：直接 Bucket=VECTOR_BUCKET 的 COS list_objects
# 新代码：
keys = await asyncio.to_thread(
    self._vector_service.list_keys_by_prefix, index_name, key_prefix
)
```

---

## 7. CosVectorStorage 说明

实现上就是把当前的 COS SDK 调用（`client.query_vectors` / `client.put_vectors` / `client.delete_vectors`）封装进 `CosVectorStorage`，保持 `VectorStorage` 接口。

vfs_indexer 的 `list_objects` 在 COS 后端也改用 `list_keys_by_prefix`（底层调 COS `list_objects`）。

多桶管理逻辑（`_resolve_bucket` / `_resolve_bucket_for_write` / `_create_next_bucket` 等）留在 `CosVectorStorage` 内部，对调用方透明。

`VECTOR_BUCKET` 常量保留向后兼容。

---

## 8. 迁移脚本

`scripts/migrate_cos_to_sqlite.py`：
1. 列出 COS 所有 index
2. 对每个 index：`list_vectors` → `SqliteVecStorage.put()`
3. 执行 `UPDATE vec_indexes SET vector_count = (SELECT COUNT(*) FROM vec_entries WHERE index_name = vec_indexes.index_name)` 修正计数
4. 对比 COUNT(*) 验证一致

可选：不迁移，新旧共存。

---

## 9. 边界情况

| 场景 | 处理方式 |
|------|---------|
| asyncio 阻塞 | `asyncio.to_thread()` 包裹所有 storage 调用 |
| sqlite-vec 未安装 | `pip install`，无系统依赖 |
| data/ 目录不存在 | `os.makedirs(..., exist_ok=True)` |
| 表名含 `]` 字符 | 白名单校验拒绝，配合 `_escape_table()` 双写 `]]` |
| 同时创建同个 index | `with self._conn:` 事务 + `INSERT OR IGNORE` 幂等 |
| LIKE 通配符 | `replace('%', '\\%')` + `ESCAPE '\\'` |
| MATCH 空结果 | 返回 `[]`，上层自行处理 |
| index 长度超限 | `_validate_index` 拒绝 len > 100 |
| index 名含非法字符 | `_validate_index` 正则拒绝 |
| query 未注册的 index | `_get_table_by_index` 抛 KeyError→返回 `[]` |
| filter 导致 top_k 不足 | fetch_k = top_k * 3 留余量 |
| 缺 drop_index | TODO：后续实现 DROP TABLE + 删 vec_indexes 行 |
| 迁移后 vector_count 不准 | 迁移脚本执行 UPDATE 修正 |

---

## 9. 文件变更清单

| 文件 | 改动 |
|------|------|
| `config.py` | + `VECTOR_STORAGE_BACKEND`, `VECTOR_SQLITE_PATH` |
| `services/vector_search_service.py` | + `VectorStorage` ABC + `CosVectorStorage` + `SqliteVecStorage` |
| | `VectorSearchService` 改 `self.storage = _create_storage()` |
| | 所有 I/O 方法改为 `asyncio.to_thread(self.storage.xxx, ...)` |
| `services/vfs_indexer.py` | `_delete_file_vectors` 改用 `self._vector_service.storage.list_keys_by_prefix()` |
| `services/vfs_indexer.py` | 移除 `VECTOR_BUCKET` import（已从多桶改造时处理） |
| `requirements.txt` | + `sqlite-vec>=0.1.0` |
