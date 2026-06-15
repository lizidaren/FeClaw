# Rerank 测试报告

## 概览

对 Qwen3-Reranker 在高考真题 + 课本知识库上的效果进行了三轮对比测试。

## 测试脚本

| 文件 | 目的 | 参数 |
|------|------|------|
| `test_rerank_v1_basic.py` | 基础对比：15 场景，纯向量 top5 vs 向量 top50→Rerank | top_k=50, max_chars=3000 |
| `test_rerank_v2_prefix_test.py` | 前缀测试：8 组配对查询，对比"课本"vs"高考真题"前缀效果 | top_k=50, max_chars=3000 |
| `test_rerank_v3_optimized.py` | 优化后：top100 + 更宽松截断 + 更准确 token 估算 | top_k=100, max_chars~6300 |

## 核心代码（可复用）

### Qwen3-Rerank API 调用

```python
RERANK_URL = 'https://dashscope.aliyuncs.com/compatible-api/v1/reranks'

def rerank(query, documents, top_n=5):
    texts = [smart_truncate(d.get('text','')) for d in documents]
    payload = {
        'model': 'qwen3-rerank',
        'query': query,
        'documents': texts,
        'top_n': min(top_n, len(texts)),
    }
    headers = {
        'Authorization': f'Bearer {QWEN_API_KEY}',
        'Content-Type': 'application/json',
    }
    resp = httpx.post(RERANK_URL, json=payload, headers=headers, timeout=30)
    data = resp.json()
    if 'output' in data and 'results' in data['output']:
        return data['output']['results']
    elif 'results' in data:
        return data['results']
    return []
```

### 智能截断（优化版）

```python
# Token 估算：中文 ~1.8 chars/token
MAX_TOKENS = 3500  # rerank 单条上限 4000，留 500 给 query
CHARS_PER_TOKEN = 1.8
MAX_CHARS = int(MAX_TOKENS * CHARS_PER_TOKEN)  # ~6300

def smart_truncate(text):
    if len(text) <= MAX_CHARS:
        return text
    head_len = 1000
    tail_len = 800
    middle_max = MAX_CHARS - head_len - tail_len
    middle_start = head_len
    middle_end = len(text) - tail_len
    if middle_end <= middle_start:
        return text[:head_len] + text[-tail_len:]
    middle_text = text[middle_start:middle_end]
    if len(middle_text) > middle_max:
        mid_head = middle_text[:middle_max // 2]
        mid_tail = middle_text[-(middle_max - middle_max // 2):]
        middle_text = mid_head + '\n...(snip)...\n' + mid_tail
    return text[:head_len] + '\n...\n' + middle_text + '\n...\n' + text[-tail_len:]
```

## 结果汇总

### V1: 基础对比（15 场景）

| 指标 | 值 |
|------|:----:|
| 平均重合度 | 2.2/5 |
| #1 结果变化 | 7/15 |
| Rerank 平均耗时 | 0.50s |

### V2: 显式前缀测试（8 对）

| 方法 | 正确响应前缀 |
|------|:----------:|
| 纯向量搜索 | 2/8 ❌ |
| Rerank | **6/8** ✅ |

Rerank 能理解"课本习题"≠"高考真题"的意图差异，纯向量几乎无感。

### V3: 优化版（top-100 + 宽裕截断）

| 指标 | V1 | V3 |
|------|:--:|:--:|
| 平均重合度 | 2.2/5 | 2.3/5 |
| top_k | 50 | 100 |
| 被截断文档 | 较多 | **3/1500** |
| 单次成本 | ~$0.003 | ~$0.005 |

## 关键发现

1. **top 100 vs top 50 差异极小**（2.2vs2.3）——最相关内容基本在 top 50 内
2. **截断 6300 chars（~3500 tok）足够覆盖 99.8% 文档**
3. **rerank 主要在同来源内重排**（课本vs课本、高考vs高考），跨来源调整较少
4. **rerank 对显式前缀有明确响应**（6/8 vs 2/8），能理解用户意图
5. 单次成本极低（~$0.005），速度可接受（~0.5s）

## 建议

1. 先做索引分离（gaokao vs textbook），rerank 在干净的候选集上效果更好
2. top 50 即可（top 100 性价比低）
3. 截断用 ~6300 chars / ~3500 tok（头 1000 + 尾 800 + 中间保留）
4. Qwen3-Rerank 使用 Dashscope 兼容 API，共用 QWEN_API_KEY

## API 参数速查

| 参数 | 值 |
|------|:----:|
| 模型 | qwen3-rerank |
| 单条最大输入 | 4,000 token |
| 最大文档数 | 500 |
| 请求最大 Token | 120,000 |
| 价格 | $0.1/百万 token |
| 免费额度 | 100 万 token（激活后 90 天） |
| 计费公式 | Query × N + Σ文档 ≤ 120,000 |
