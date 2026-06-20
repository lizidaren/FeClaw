# Phase 4 Engine Report — Group Chat Engine

**Date**: 2026-06-20
**Status**: ✅ Implementation Complete

---

## What Was Implemented

### 1. `models/group.py` — Group Database Models

Three new SQLAlchemy models (separate from existing `models/database.py` to keep things clean):

- **`Group`**: Core group entity with `id` (UUID), `name`, `announcement`, `owner_user_id`, `settings` (JSON), `context_isolation`, `max_rounds`, soft-delete `deleted_at`.
- **`GroupMember`**: Agent membership with `group_id`, `agent_hash`, `role` ("owner"/"member"), `is_silent` flag, `joined_at`.
- **`GroupMessage`**: Chronological message log with `group_id`, `sender_type` ("user"/"agent"), `sender_hash`, `content`, `message_type`, `mentions` (JSON), `round` (dispatch round counter).

Tables are auto-created via `Base.metadata.create_all()` on startup (imported in `main.py` lifespan before `init_db()`).

### 2. `services/group_service.py` — Dispatch Architecture

**`GroupDispatchService`** singleton implementing the Dispatch pattern:

| Method | Purpose |
|--------|---------|
| `on_message()` | Entry point — saves `GroupMessage`, fires `dispatch_to_members` |
| `dispatch_to_members()` | Round-robin dispatch: for each non-excluded member where `should_wake` returns True, spawns `agent_reply` task |
| `should_wake()` | `round==0` → always wake; `is_silent` → skip; otherwise → wake |
| `agent_reply()` | Builds context, calls LLM, handles `NO_REPLY` magic string (marks member silent), saves reply, fires next dispatch round |
| `build_context()` | Loads chronological `GroupMessage` history for group, compacts if >15% of model window, loads agent persona from `agent_init_service` |
| `_build_group_prompt()` | Builds system prompt with time header, persona, message history, and `NO_REPLY` rule |
| `_call_llm()` | Calls `LLMService.chat()` directly (not via `ChatService` — group chat has no tool calls) |
| `_push_to_clients()` | Pushes new agent replies via `DesktopConnectionManager` (silently skipped if Desktop not connected) |
| `create_group()` / `add_member()` / `remove_member()` / `get_messages()` / `get_group()` / `get_member()` / `list_user_groups()` | CRUD helpers |

**Key design decisions**:
- `asyncio.create_task()` for fire-and-forget dispatch — no blocking
- `NO_REPLY` magic string from LLM marks `is_silent = True` in DB
- `MAX_ROUNDS = 100` guard prevents infinite dispatch loops
- `round=0` always triggers all agents; subsequent rounds skip silent members
- Context compaction at 15% of 110K token window (~16.5K tokens) preserves recent messages

### 3. `routers/group.py` — REST API

Full CRUD API under `/api/groups`:

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/groups` | Create group with name + optional agent members |
| GET | `/api/groups` | List all groups for current user |
| GET | `/api/groups/{group_id}` | Get group details |
| PATCH | `/api/groups/{group_id}` | Update group settings/announcement |
| DELETE | `/api/groups/{group_id}` | Soft-delete group (owner only) |
| GET | `/api/groups/{group_id}/members` | List members |
| POST | `/api/groups/{group_id}/members` | Add agent member (owner only) |
| DELETE | `/api/groups/{group_id}/members/{agent_hash}` | Remove member (owner only) |
| GET | `/api/groups/{group_id}/messages` | Paginated message history (newest first) |
| POST | `/api/groups/{group_id}/messages` | Send user message → triggers dispatch |

All endpoints require JWT auth (`get_current_user_id`). Owner-only operations (delete, add/remove members) verify `owner_user_id == user_id`.

### 4. `main.py` — Router Registration

- Added `from routers.group import router as group_router`
- Added `app.include_router(group_router)` after `user_router`
- Added `from models.group import Group, GroupMember, GroupMessage` before `init_db()` so `Base.metadata.create_all()` covers the new tables

---

## Files Changed

| File | Change |
|------|--------|
| `models/group.py` | **NEW** — Group, GroupMember, GroupMessage models |
| `services/group_service.py` | **NEW** — GroupDispatchService |
| `routers/group.py` | **NEW** — REST API router |
| `main.py` | **MODIFIED** — register group router + import Group models |

---

## Verified Code Patterns

- **DB access**: Used `SessionLocal()` for transient service-level DB sessions (same pattern as `ChatService.get_session()`)
- **Auth**: `get_current_user_id` dependency for all routes
- **Router pattern**: Follows `routers/user.py` — `APIRouter(prefix="/api/groups", tags=["Groups"])`
- **Response models**: Pydantic `BaseModel` classes for all request/response bodies
- **JSONResponse**: Used for endpoints returning raw dicts
- **Error handling**: `HTTPException(status_code=404/400/403)` for not found / bad request / forbidden

---

## Pending / Notes

1. **WS Push limitation**: `_push_to_clients()` uses `DesktopConnectionManager.send()` which sends to the single connected Desktop. For multi-client group push, a proper group-room WS manager would be needed (separate from Desktop WS). Currently silently skipped if Desktop not connected — not a hard requirement.

2. **`list_user_groups()` scope**: Currently only returns groups where user is `owner_user_id`. Should be extended to also find groups where user's agents are members (requires cross-referencing `GroupMember` table). The REST API is functional for owners.

3. **Group WS endpoint**: No dedicated WebSocket endpoint for real-time group messages. Clients can poll `GET /api/groups/{id}/messages`. SSE or WS push can be added in a future phase.

4. **`agent_service.py`**: Not found at `services/agent_service.py` — the `agent_init_service` is used for agent creation/initialization instead.

5. **Model window compaction**: Uses `estimate_tokens` from `message_compactor` to count tokens. Assumes 110K context window.
