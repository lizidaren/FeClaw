# Phase 2A: VFS File Manager API Endpoints — Implementation Report

## Files Modified

- `routers/user.py` — added 11 endpoints to the existing `user_router`
- `models/agent_profile.py` — added 3 new columns: `is_pinned`, `is_dnd`, `permission_mode`

## All 11 Endpoints Implemented

### 1. GET `/api/user/agents/{hash}/vfs`
- Lists directory contents at given VFS `path` (default `/`)
- Calls `storage_service.list_objects(cos_prefix)` directly
- Returns `[{name, type: "dir"|"file", size, mtime, content_type}]`
- Deduplicates by name; sorts dirs first then alphabetically

### 2. GET `/api/user/agents/{hash}/vfs/url`
- Query params: `path`, `mode=view|download` (default `download`), `expires` (1–604800s, default 86400)
- Constructs COS key from `TENCENT_COS_PREFIX/agents/{hash}{vpath}`
- Uses `storage_service.get_object_public_url(key)` + `storage_service.generate_presigned_get_url(url, expired)`
- Returns `{url, method:"GET", expires_at, mode, key}`
- **Limitation**: Tencent COS `get_presigned_url` does not natively support `response-content-disposition` query param. `mode` is returned as a hint for the client; inline viewing relies on Content-Type header handling by the browser.

### 3. POST `/api/user/agents/{hash}/vfs/url-upload`
- Body: `{path, content_type}` (default `application/octet-stream`)
- Query param: `expires` (60–86400s, default 3600)
- Uses `storage_service.generate_presigned_put_url(cos_key, expired)`
- Returns `{url, method:"PUT", expires_at, key, content_type}`

### 4. POST `/api/user/agents/{hash}/vfs/mkdir`
- Body: `{path}` (e.g. `/workspace/images`)
- COS has no real directories — creates a zero-byte object with trailing `/` via `put_object(cos_key + "/", b"")`

### 5. DELETE `/api/user/agents/{hash}/vfs/rm`
- Query param: `path`
- For directories (path ending in `/`): verifies directory is empty before deleting
- Uses `storage_service.delete_file_by_key(cos_key)`

### 6. POST `/api/user/agents/{hash}/vfs/mv`
- Body: `{from_path, to_path}`
- Implementation: `get_file_content` → `put_object` → `delete_file_by_key`
- Does **not** use COS `copy_object` API (not exposed in `CosStorage`; read+write+delete is the fallback)

### 7. PATCH `/api/user/agents/{hash}/vfs/permissions`
- Body: `{path, permission}` where permission is `"read"` | `"readwrite"` | `"none"`
- Uses `PermissionService(agent_hash=hash, db=db).grant_permission(path, permission)`
- Writes to `file_permissions` table

### 8. POST `/api/user/agents/{hash}/vfs/events`
- Body: `{events: [{type, path, timestamp}]}`
- Currently a no-op: logs each event and returns 200
- Future: index for change-feed/webhooks

### 9. PATCH `/api/user/agents/{hash}/settings`
- Body: `{alias?, is_pinned?, is_dnd?, permission_mode?}`
- `alias` → `AgentProfile.name`; new DB columns `is_pinned`, `is_dnd`, `permission_mode` added to `models/agent_profile.py`
- Returns updated fields

### 10. GET `/api/user/agents/{hash}/apps`
- Returns `{"apps": []}` (placeholder; no apps service exists yet)

### 11. GET/PUT `/api/user/agents/{hash}/config`
- GET: returns `{persona, tools, config}` using `agent_init_service.load_agent_*` methods
- PUT: accepts `{persona?, tools?, config?}`; delegates to `agent_init_service.save_agent_*` methods
- PUT returns partial-success if any field fails

## Presigned URL TTL Configuration

| Operation | Default TTL | Configurable Range | Query Param |
|---|---|---|---|
| GET (download/view) | 86400s (1 day) | 1–604800s (7 days) | `?expires=` |
| PUT (upload) | 3600s (1 hour) | 60–86400s (1 day) | `?expires=` |

TTL is passed as `expired` to `generate_presigned_get_url` / `generate_presigned_put_url`.

## COS Operations

### Key construction
```
VFS /workspace/images/photo.png
  → COS key: {TENCENT_COS_PREFIX}agents/{hash}/workspace/images/photo.png
  → e.g. feclaw/agents/ab12/workspace/images/photo.png

VFS / (root)
  → COS prefix: {TENCENT_COS_PREFIX}agents/{hash}/
  → e.g. feclaw/agents/ab12/
```

### Copy + Delete (mv)
COS does not expose a `copy_object` method in `CosStorage`. The mv implementation uses:
1. `storage.get_file_content(from_key)` — reads source bytes
2. `storage.put_object(to_key, content)` — writes to destination
3. `storage.delete_file_by_key(from_key)` — removes source

Limitation: for very large files this is inefficient. A future improvement would call `client.copy_object` directly on the COS S3 client.

### Directory representation
COS has no real directories. Directories are represented by:
- A zero-byte object with a trailing `/` in the key (for `mkdir`)
- The prefix-based listing (for `ls`)

## Unclear / TODO Items

1. **`mode=view` inline disposition**: Tencent COS `get_presigned_url` SDK method does not support `response-content-disposition` as a query parameter. The current implementation returns the `mode` as a hint for the client but cannot enforce inline vs attachment behavior server-side. If this is critical, we would need to use a signed URL with custom headers or a different COS API.

2. **DB migration for new columns**: `is_pinned`, `is_dnd`, `permission_mode` columns were added to `models/agent_profile.py`. The existing SQLite DB will need these columns created on next startup if `Base.metadata.create_all()` is called (which only creates new tables, not new columns in existing tables). A manual ALTER TABLE or Alembic migration may be needed for production.

3. **`/api/user/agents/{hash}/vfs/events`**: Currently a no-op logger. A future implementation would store events in a DB table for indexing/hooks.

4. **`/api/user/agents/{hash}/apps`**: Returns empty list. The apps service does not yet exist.

5. **COS copy for mv**: Uses read+write+delete instead of native `copy_object`. Works correctly but not optimal for large files.

## Existing Bug Discovered

The **existing** endpoints in `routers/user.py` have a path duplication issue: the router has `prefix="/api/user"` AND the decorators also include `/api/user/`, resulting in paths like `/api/user/api/user/permissions` instead of `/api/user/permissions`. Affected endpoints:
- `get_user_permissions` → `/api/user/api/user/permissions` (should be `/api/user/permissions`)
- `create_agent` → `/api/user/api/user/agents` (should be `/api/user/agents`)
- `get_agent_by_hash` → `/api/user/api/user/agents/{agent_hash}` (should be `/api/user/agents/{agent_hash}`)

The **Phase 2A endpoints are correctly implemented** without this duplication (verified with `python3 -c "import routers.user; ...` showing correct paths).
