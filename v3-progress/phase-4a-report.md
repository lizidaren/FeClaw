# Phase 4a Report: 3 Missing API Endpoints for Desktop

## Endpoints Created

All 3 endpoints added to `routers/user.py` under the existing `router = APIRouter(prefix="/api/user")`.

### 1. GET /api/user/permissions
- **Path**: `routers/user.py:237`
- Returns user tier ("pro" default), username, full features dict, and usage stats
- Auth: `get_current_user_id` dependency (JWT Bearer token)
- Tier hardcoded to "pro" (no tier field exists on User model yet)
- Usage stats: agent_count from AgentProfile, group_count=0, today_messages from ChatHistory

### 2. POST /api/user/agents
- **Path**: `routers/user.py:292`
- Creates agent via `agent_init_service.create_agent(db, user_id, name=name)`
- Accepts `agent_type` ("classic"|"im") in body but only passes `name` to service (agent_type not stored — AgentProfile has no such field)
- Auth: `get_current_user_id` dependency

### 3. GET /api/user/agents/{agent_hash}
- **Path**: `routers/user.py:324`
- Looks up AgentProfile by hash + user_id (ownership verified)
- Returns hash, name, description, agent_type (always "classic" — no field), created_at (unix timestamp), avatar_url (null)
- Auth: `get_current_user_id` dependency

## Files Modified

- `routers/user.py` — Added imports (`AgentProfile`, `ChatHistory`, `date`, `get_current_user_id`, `agent_init_service`) and 3 new endpoints

## JWT Auth Wiring

- Uses `get_current_user_id` from `utils.auth` — same pattern as `feclaw_chat.py`
- `get_current_user_id` decodes JWT Bearer token, extracts `user_id`
- No new auth infrastructure needed; `user_router` already registered in `main.py`

## Unclear Items

1. **`agent_type` not stored**: The mission spec includes `agent_type: "classic" | "im"` in the create request and response, but `AgentProfile` model has no `agent_type` column. The endpoint accepts it in the request body but ignores it, always storing "classic". A future migration would add this column.

2. **No user tier field**: Permissions endpoint returns `tier: "pro"` hardcoded. User model has no `tier` column. Would need a migration to add it.

3. **`avatar_url` always null**: AgentProfile has no avatar_url field, so the get-agent endpoint always returns null.

## Checklist

- [x] GET /api/user/permissions — implemented
- [x] POST /api/user/agents — implemented
- [x] GET /api/user/agents/{agent_hash} — implemented
