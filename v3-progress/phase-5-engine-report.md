# Phase 5 Engine Report — Group Moments (群广场)

**Date**: 2026-06-20
**Status**: ✅ Implementation Complete

---

## What Was Implemented

### 1. `models/group.py` — GroupMoments Model

Added `GroupMoments` table to the existing `models/group.py` file (co-located with `Group`, `GroupMember`, `GroupMessage`):

```python
class GroupMoments(Base):
    __tablename__ = "group_moments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String(36), nullable=False, index=True)
    agent_hash = Column(String(4), nullable=True)
    kind = Column(String(32), nullable=False)  # "task_done"|"file_changed"|"analysis"|"consensus"|"manual"
    title = Column(String(200))
    content = Column(Text)
    attachments = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
```

Indexes on `group_id` and `created_at` for efficient pagination queries.
Auto-created via `Base.metadata.create_all()` on startup (imported in `main.py` lifespan alongside other group models).

### 2. `services/moments_service.py` — Moments Service

New **`MomentsService`** singleton providing:

| Method | Purpose |
|--------|---------|
| `create_moment()` | Create and persist a GroupMoments record |
| `get_moments()` | Paginated list for a group (newest first, optional `before` timestamp) |
| `get_user_moments()` | Aggregate moments across all groups the user owns/is member of |
| `delete_moment()` | Delete (owner only) — verifies user owns the group |
| `push_moments_event()` | WS push to Desktop via `DesktopConnectionManager` |
| `auto_publish()` | Convenience: create + async WS push in one call (for hooks) |

**WS payload format** (`moments_event` type):
```python
{
  "type": "moments_event",
  "group_id": "xxx",
  "moment": {
    "id": "...",
    "agent_hash": "abc",
    "agent_name": "李老师",  # resolved from AgentProfile
    "kind": "file_changed",
    "title": "修改了文件三角公式.txt",
    "content": "在群组中更新了文件 /workspace/三角公式.txt",
    "attachments": [],
    "created_at": 1710000000
  }
}
```

### 3. `services/tools/moments_tools.py` — MomentsToolsMixin

New mixin (added as first base class in `AgentToolsService` MRO) providing:

#### `create_post` tool
```python
@tool(description="将内容发布到当前群组的朋友圈/动态中...", category="general")
def create_post(title: str, content: str, attachments: List[dict] = None) -> dict
```
- Checks `self._group_id` — returns error if not in group context
- Saves to `GroupMoments` with `kind="manual"`
- Fires async WS push to Desktop
- Returns `{"status": "ok", "moment_id": "...", "message": "已发布到群组动态"}`

#### Auto-publish hooks

**`file_write` override** (wraps `FileOpsMixin.file_write`):
- Calls original via `await super().file_write(path, content)`
- On success (`OK`/`已写入`/`written`) + `_group_id` set → publishes `file_changed` moment

**`spawn_subagent` override** (wraps `AIToolsMixin.spawn_subagent`):
- Calls original via `super().spawn_subagent(...)`
- On non-error result + `_group_id` set → publishes `analysis` moment

Both hooks use `moments_service.auto_publish()` (fire-and-forget, WS push async).

#### MRO note
`AgentToolsService` MRO puts `MomentsToolsMixin` first, so:
- `MomentsToolsMixin.file_write` → `super()` → `FileOpsMixin.file_write` ✓
- `MomentsToolsMixin.spawn_subagent` → `super()` → `AIToolsMixin.spawn_subagent` ✓

### 4. REST API Routes

#### Group-scoped routes (`routers/group.py`, prefix `/api/groups`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/{group_id}/moments` | List group moments (owner auth, pagination via `?before=ts&limit=50`) |
| DELETE | `/{group_id}/moments/{moment_id}` | Delete moment (owner only) |

#### User-level routes (`routers/user.py`, prefix `/api/user`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/moments` | Aggregate moments across all user's groups (`?group_id=x` to filter) |
| POST | `/moments` | Manually create a moment for a specific group (body: `group_id`, `kind`, `title`, `content`, `attachments`) |

### 5. `services/tools/base.py` — `_group_id` support

Added optional `group_id` parameter to `AgentToolsServiceBase.__init__`:
```python
def __init__(self, agent_hash: str, group_id: str = None):
    ...
    self._group_id = group_id  # 当前群组上下文（ moments auto-publish 用）
```

Currently all existing callers (`ChatService`, `wechat_channel_service`, etc.) use the default `None` — auto-publish hooks will only fire when `_group_id` is explicitly set (future: when group-context tool execution is added to `GroupDispatchService`).

### 6. `main.py` wiring

- Added `GroupMoments` import to lifespan startup import list (triggers `Base.metadata.create_all()`)
- No new router registration needed (group moments use existing `/api/groups` router; user moments use existing `/api/user` router)

---

## Key Design Decisions

1. **Mixin override pattern for hooks**: `MomentsToolsMixin` overrides `file_write` and `spawn_subagent` via `super()` calls, preserving all original behavior while adding moments side-effects. Works correctly with Python MRO since it's placed first in `AgentToolsService`.

2. **`_group_id` opt-in**: Auto-publish hooks silently skip when `_group_id` is `None` (1:1 sessions). This avoids null-checking everywhere while keeping hooks inactive unless group context is explicitly established.

3. **WS push fire-and-forget**: All WS pushes use `asyncio.create_task()` so they never block the tool return. Failures are logged at `debug` level only.

4. **Moment kinds**: `manual` (user/agent explicit post), `file_changed` (auto after write), `analysis` (auto after spawn_subagent), `task_done`, `consensus` (reserved for future consensus达成 hook).

5. **agent_name resolution**: WS push resolves `agent_name` from `AgentProfile.name` at push time (not stored in DB), keeping the moment record lean.

---

## Limitations / Future Work

- **`GroupDispatchService.agent_reply` doesn't use tools**: Currently group replies only do LLM calls (no `AgentToolsService`). The `create_post` tool and auto-publish hooks only work in 1:1 sessions until group-context tool execution is added.
- **No `task_done` or `consensus` auto-publish hooks**: These kinds are defined but no triggers implemented yet.
- **`_group_id` must be set manually**: Future work: wire `_group_id` into `ChatService` when running in group context (via `GroupDispatchService` calling into `ChatService`).
