# 分享页选中标记 → 引用令牌 设计方案

## 目标

用户在 MD 分享页选中一段文字 → 点击"标记" → 生成 `[reference:abc123]` 短令牌 → 发给 Agent → Agent 解析令牌、拿到原文和上下文，进行针对性解答。

## 数据流

```
用户选中文字 → 浮动"📌 标记"按钮 → 生成短 hash → 存库
用户复制 [reference:xxx] 发给 Agent → Agent 解析 → 查库 → 注入 prompt
```

## 数据库模型

```python
# models/database.py

class ShareReference(Base):
    """分享页引用标记"""
    __tablename__ = "share_references"

    id = Column(Integer, primary_key=True)
    ref_hash = Column(String(8), unique=True, index=True, nullable=False)  # base62 短 hash
    share_hash = Column(String(16), nullable=False, index=True)             # 所属分享链接
    vfs_path = Column(String(512), nullable=False)                         # 源文件路径
    selected_text = Column(Text, nullable=False)                           # 选中的文字
    context_before = Column(Text, default="", server_default="")           # 选中前 200 字
    context_after = Column(Text, default="", server_default="")            # 选中后 200 字
    created_at = Column(DateTime, default=datetime.utcnow)
```

## 前端（share 页面 JS）

### 选择监听

- 监听 `mouseup` / `touchend` 事件
- `window.getSelection()` 检测是否有非空白选中文本
- 选中区域上方显示浮动按钮 `📌 标记此段`
- 点击浮动按钮触发标记请求

### 标记请求

```
POST /api/share/reference
Content-Type: application/json

{
  "share_hash": "abc123def456",
  "vfs_path": "/workspace/agent/some-file.md",
  "selected_text": "选中的文字内容",
  "context_before": "选中前 200 字...",
  "context_after": "选中后 200 字..."
}
```

### 响应

```json
{
  "ref_hash": "xK9mP2qR"
}
```

### 交互反馈

- 弹出 Toast："📌 已标记！复制以下内容发给 AI：`[reference:xK9mP2qR]`"
- 自动复制到剪贴板（`navigator.clipboard.writeText()`）
- 按钮变为 ✅ 已标记 状态，1.5 秒后消失

## 后端 API

```python
# routers/share_reference.py

@router.post("/api/share/reference", status_code=201)
async def create_share_reference(
    data: ShareRefRequest,
    db: Session = Depends(get_db),
):
    """创建分享页引用标记"""
    ref_hash = _generate_ref_hash(db)  # 碰撞重试
    ref = ShareReference(
        ref_hash=ref_hash,
        share_hash=data.share_hash,
        vfs_path=data.vfs_path,
        selected_text=data.selected_text,
        context_before=data.context_before,
        context_after=data.context_after,
    )
    db.add(ref)
    db.commit()
    return {"ref_hash": ref_hash}


@router.get("/api/share/reference/{ref_hash}")
async def get_share_reference(ref_hash: str, db: Session = Depends(get_db)):
    """通过 ref_hash 查询引用内容（Agent 调用）"""
    ref = db.query(ShareReference).filter(ShareReference.ref_hash == ref_hash).first()
    if not ref:
        raise HTTPException(status_code=404, detail="引用不存在")
    return {
        "selected_text": ref.selected_text,
        "context_before": ref.context_before,
        "context_after": ref.context_after,
        "vfs_path": ref.vfs_path,
    }
```

### 短 hash 生成

```python
import secrets

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

def _generate_ref_hash(db) -> str:
    """生成 8 位 base62 短 hash，碰撞时重试"""
    for _ in range(10):
        h = "".join(secrets.choice(BASE62) for _ in range(8))
        if not db.query(ShareReference).filter(ShareReference.ref_hash == h).first():
            return h
    # 极端低概率：10 次碰撞，追加随机后缀
    return h + secrets.choice(BASE62)
```

- 8 位 base62，空间 `62^8 ≈ 218万亿`
- 使用 `secrets.choice`（密码学安全随机，非 `random`）
- 碰撞概率 ≈ 0

## Agent 端 Tool

Agent 在 system prompt 中自动获得能力提示（已在 chat_service.py 中），当用户消息包含 `[reference:xxx]` 时调用 resolve_share_reference 工具。

```python
# services/share_reference_service.py

@tool
async def resolve_share_reference(ref_hash: str) -> str:
    """解析分享页引用令牌，返回选中的原文和上下文。

    当用户提到 [reference:xxx] 格式的引用时调用此工具。
    
    Args:
        ref_hash: 引用令牌 hash（如 xK9mP2qR）
    
    Returns:
        选中文本 + 前后文，供注入 prompt
    """
    from models.database import SessionLocal, ShareReference
    
    db = SessionLocal()
    try:
        ref = db.query(ShareReference).filter(
            ShareReference.ref_hash == ref_hash
        ).first()
        if not ref:
            return "错误：未找到该引用（ref_hash: {ref_hash}）"
        
        parts = []
        if ref.context_before:
            parts.append(f"【前文】\n{ref.context_before}")
        parts.append(f"【选中内容】\n{ref.selected_text}")
        if ref.context_after:
            parts.append(f"【后文】\n{ref.context_after}")
        parts.append(f"（来源：{ref.vfs_path}）")
        
        return "\n\n".join(parts)
    finally:
        db.close()
```

### 上下文截取策略

在前端发送请求时，从**原始 MD 内容**（marked.js 渲染前的纯文本）中截取：

```javascript
function extractContext(fullText, selectedText, contextChars = 200) {
    const idx = fullText.indexOf(selectedText);
    if (idx === -1) return { context_before: "", context_after: "" };
    
    const start = Math.max(0, idx - contextChars);
    const end = Math.min(fullText.length, idx + selectedText.length + contextChars);
    
    return {
        context_before: fullText.slice(start, idx),
        context_after: fullText.slice(idx + selectedText.length, end),
    };
}
```

注意：
- 使用**渲染前的原始 MD 纯文本**，而非渲染后的 HTML（避免标签干扰）
- 标记请求时前端把上下文字段一起 POST，后端不重复计算
- `context_before`/`after` 截取后无需再渲染，API 返回的就是明文

## 安全性考虑

- **跨 Agent 可查**：任何 Agent 可用 ref_hash 查询，但只返回选中的文本片段，不是完整文档
- **无认证要求**：引用标记是公开的（分享页本身就是公开的），GET 端点的 ref_hash 本身充当了访问凭证
- **数据量**：每条引用 < 1KB，即使大量使用也几乎无存储压力
- **hash 碰撞预防**：10 次重试 + secrets 随机，碰撞概率可忽略

## 文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `models/database.py` | 改 | 新增 ShareReference 模型 |
| `routers/share_reference.py` | 新 | 创建/查询引用 API |
| `services/share_reference_service.py` | 新 | Agent tool + 业务逻辑 |
| `static/js/share-reference.js` | 新 | 分享页前端交互 |
| `routers/share.py` | 改 | 两个路由的 HTML 模板加载新 JS |

## 实现顺序

1. 数据库模型 + migration
2. 后端 API（CRUD）
3. Agent tool
4. 前端 JS + 模板修改
5. E2E 测试
