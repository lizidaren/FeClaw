# Phase 8 Engine Report — Upload Session + Upload Done + Upload Web Page

## Overview

Implemented a phone-to-desktop upload flow where:
1. Desktop creates an upload session → gets `presigned_put_url`
2. Phone receives URL → PUTs file directly to COS via presigned URL
3. Phone calls `upload_done` → FeClaw pushes `upload_complete` WS event to Desktop
4. Phone also serves `upload.html` (the upload page itself) with the session params in query string

## Files Created

### `services/upload_service.py` (NEW)

`UploadService` — in-memory session manager with 10-minute TTL.

| Method | Description |
|--------|-------------|
| `create_session(user_id)` | Generates session_id (8-char hex), builds COS key `feclaw/uploads/{session_id}/file`, generates presigned PUT URL via `CosStorage.generate_presigned_put_url`. Returns `(session_id, presigned_put_url)` |
| `confirm_upload(session_id, filename)` | Marks session complete, generates presigned GET URL via `CosStorage.generate_presigned_get_url`, then deletes session from memory. Returns `presigned_get_url` or `None` |
| `get_session(session_id)` | Returns `UploadSession` if valid and not expired |

`UploadSession` dataclass fields: `session_id`, `user_id`, `presigned_put_url`, `cos_key`, `filename`, `completed`, `created_at`.

Global singleton: `upload_service`.

### `routers/upload.py` (NEW)

| Endpoint | Description |
|----------|-------------|
| `POST /api/desktop/upload_session` | Body: `{user_id}` → calls `upload_service.create_session()`, returns `{session_id, presigned_put_url, expires_in}` |
| `POST /api/desktop/upload_done` | Body: `{session_id, filename}` → calls `confirm_upload()`, then `await manager.send({type: "upload_complete", session_id, filename, presigned_get_url})` |

### `static/upload.html` (NEW)

Minimal mobile-friendly upload page. Reads `presigned_url` and `session_id` from URL query params. On file selection:
1. `PUT` to presigned URL with `fetch`
2. `POST /api/desktop/upload_done` with `{session_id, filename}`
3. Shows success/error message

Pure vanilla HTML+JS, no dependencies.

### `main.py` (MODIFIED)

- Added `from routers.upload import router as upload_router`
- Added `app.include_router(upload_router)` (unconditional, not behind `DESKTOP_ENABLED` flag)

## Data Flow

```
Desktop                          FeClaw                          Phone
  |                                |                               |
  |-- POST /upload_session ------->|                               |
  |<-- {session_id, presigned_url }-|                               |
  |                                |                               |
  |  (sends URL + session_id to phone)                            |
  |                                |                               |
  |                                |<-- PUT file to presigned_url -|
  |                                |                               |
  |                                |<-- POST /upload_done --------|
  |                                |                               |
  |  WS upload_complete ---------->|                               |
  |                                |                               |
```

## WS Push Payload

```json
{
  "type": "upload_complete",
  "session_id": "abc12345",
  "filename": "photo.jpg",
  "presigned_get_url": "https://..."
}
```

## Edge Cases

- Session not found or expired: `upload_done` returns HTTP 404
- COS presigned URL generation failure: `create_session` returns empty URL (logged)
- Desktop WS not connected: `manager.send()` returns `False` (logged, not propagated to phone)
- Session TTL: 600 seconds (10 min), enforced on `confirm_upload` and `get_session`

## Dependencies

- `services.storage_service.CosStorage.generate_presigned_put_url` (line 357)
- `services.storage_service.CosStorage.generate_presigned_get_url` (found in `services/storage_service.py`)
- `routers.desktop_ws.manager` (global `DesktopConnectionManager` singleton)

## No Database Tables

All session state is in-memory. No new tables created. `init_db()` and model imports are unchanged.
