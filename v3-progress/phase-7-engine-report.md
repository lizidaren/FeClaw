# Phase 7 Engine — Search Aggregation Endpoint

## Status: Implemented

## Endpoint

**`GET /api/user/search`** in `routers/user.py`

```python
@router.get("/api/user/search")
async def search_all(
    q: str,
    sources: str = "chat,vfs,moments,textbook",
    limit: int = 10,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
```

## Response Format

```json
{
  "query": "三角函数",
  "results": {
    "chat":    { "status": "ok", "items": [...] },
    "vfs":     { "status": "ok", "items": [...] },
    "moments": { "status": "ok", "items": [...] },
    "textbook":{ "status": "ok", "items": [...] },
    "miniapps":{ "status": "ok", "items": [] },
    "local":   { "status": "ok", "items": [] }
  },
  "elapsed_ms": 450
}
```

Per-item shape:
```json
{
  "id": "msg-uuid or key",
  "agent_hash": "a1b2",
  "agent_name": "李老师",
  "snippet": "...三角函数公式总结...",
  "score": 0.89,
  "timestamp": 1710000000,
  "source": "chat" | "vfs" | "moments" | "textbook"
}
```

## Source Implementation

| Source | Method | Timeout |
|--------|--------|---------|
| `chat` | SQL LIKE on `ChatHistory.content` filtered by `user_id` | 3s |
| `vfs` | `VectorSearchService.search_public_with_quality()` on each agent's `idx-{hash}-kb` | 3s |
| `moments` | Python filter on `GroupMoments` rows from user's groups (title + content LIKE) | 3s |
| `textbook` | `VectorSearchService.search_quality_textbook()` (9 subject indexes + trends) | 3s |
| `miniapps` | Placeholder — always returns empty list | 3s |
| `local` | Placeholder — always returns empty list (Desktop will add later) | 3s |

## Key Design Decisions

1. **3s total timeout** — Uses `asyncio.wait()` on all source tasks with a shared `TIMEOUT` (not per-task). If any source times out, its entry gets `status: "timeout"` and others continue.

2. **Parallel execution** — All enabled sources launch as `asyncio.create_task()` and run concurrently via `asyncio.wait()`.

3. **Per-source status** — Each source independently returns `ok | timeout | error` so partial failures don't kill the whole response.

4. **VFS search** — Searches each of the user's agents' private KB indexes (`idx-{hash}-kb`) in parallel using `search_public_with_quality()`. No dedicated public VFS index yet.

5. **Moments search** — Fetches up to 200 recent `GroupMoments` rows for the user's owned groups and does Python-side `in` filter on `title + content`. No vector search yet.

6. **Textbook search** — Uses existing `search_quality_textbook()` which searches 9 subject-specific indexes + math-trends with Qwen subject routing.

7. **Imports** — Added `GroupMoments` to imports from `models.group`.

## Files Changed

- `routers/user.py` — Added `/api/user/search` endpoint (~130 lines) + 3 sync helper functions

## Not Implemented (Future)

- `miniapps`: AgentProfile has no `apps` field yet — returns empty
- `local`: Desktop will add local file index search later via Desktop-to-Server API
- Vector search for chat history (requires indexing ChatHistory content separately)
- `search_public_with_quality` on a "public VFS" index for shared agent knowledge
