# Audit Fix Engine Report — FeClaw

**Date:** 2026-06-21
**Status:** All CRITICAL and HIGH findings resolved

---

## 1. `routers/upload.py` — CRITICAL: JWT Auth for Upload Endpoints

**Finding:** `create_upload_session` and `upload_done` accepted `user_id` from request body, allowing any caller to impersonate any user.

**Fix Applied:**
- Replaced `request.get("user_id")` body parsing with proper JWT dependency `user_id: int = Depends(get_current_user_id)` on both endpoints
- Removed the insecure `_get_user_id_from_request` helper function (which relied on `request.state.user_id` set by middleware that was never actually applied to these routes)
- Added `from fastapi import Depends` to imports
- Added `from utils.auth import get_current_user_id` to imports

**Files Modified:**
- `routers/upload.py`

**Impact:** Both endpoints now require valid JWT Bearer token. No more user impersonation via crafted request body.

---

## 2. `services/fehub_service.py` — CRITICAL: Path Traversal Protection

**Finding:** `_snapshot_workspace` and `_copy_template` used VFS paths in COS key construction without validating against `..` path traversal attacks.

**Fix Applied:**
- Added `from pathlib import PurePosixPath` import
- Added static method `FeHubService._validate_path(vfs_path: str) -> bool` that:
  - Returns `False` if path is absolute
  - Returns `False` if `..` appears in path parts
  - Returns `True` otherwise
- Added validation in `_copy_template`: validates `target` before use, skips `rel_path` entries that fail validation
- Added validation in `_snapshot_workspace`: validates each `vfs_file_path` before constructing `dest_key`

**Files Modified:**
- `services/fehub_service.py`

**Impact:** Prevents malicious VFS paths like `../../../etc/passwd` from escaping the agent's COS namespace.

---

## 3. `services/moments_service.py` — HIGH: Wrong Import

**Finding:** `AgentProfile` was imported from `models.group` instead of `models.database`. The `Group` model file does not export `AgentProfile` — it lives in `models/database.py`. This would cause `NameError` at runtime when `push_moments_event` tried to query `AgentProfile`.

**Fix Applied:**
- Changed:
  ```python
  # Before (broken):
  from models.database import SessionLocal
  from models.group import GroupMoments, AgentProfile
  ```
  To:
  ```python
  # After (correct):
  from models.database import SessionLocal, AgentProfile
  from models.group import GroupMoments
  ```

**Files Modified:**
- `services/moments_service.py`

**Impact:** `moments_service.push_moments_event()` no longer crashes with `ImportError` when resolving agent names for WS push events.

---

## 4. `routers/upload.py` — WS Push for `upload_done`

**Finding:** The `upload_done` endpoint already pushed to the Desktop WS channel after confirming upload, but the JWT auth fix (Finding #1) ensures the `user_id` used in the push context is now properly authenticated.

**Note:** The current `DesktopConnectionManager` broadcasts to the single connected Desktop session. The push is already implemented via `manager.send(ws_push_payload)`. Further per-user WS routing would require `DesktopConnectionManager` to track connections per `user_id`, which is a separate enhancement beyond the scope of this audit fix.

**Files Modified:**
- `routers/upload.py`

---

## Summary

| # | File | Severity | Issue | Status |
|---|------|----------|-------|--------|
| 1 | `routers/upload.py` | CRITICAL | JWT auth on upload endpoints | Fixed |
| 2 | `services/fehub_service.py` | CRITICAL | Path traversal in VFS operations | Fixed |
| 3 | `services/moments_service.py` | HIGH | Wrong `AgentProfile` import | Fixed |
| 4 | `routers/upload.py` | — | WS push for `upload_done` | Already present (auth fix ensures correct user context) |

All CRITICAL and HIGH findings have been resolved. No test files or unrelated logic were modified.