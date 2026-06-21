# FeClaw Engine Phase 4-8 Architectural Audit Report

**Auditor**: Claude Code
**Date**: 2026-06-21
**Scope**: Phase 4 (Group Chat), Phase 5 (Moments), Phase 6 (FeHub VCS+Publish), Phase 7 (Search), Phase 8 (Upload Session)

---

## Summary by Severity

| Severity | Count |
|----------|-------|
| CRITICAL | 2 |
| HIGH | 6 |
| MEDIUM | 7 |
| LOW | 5 |

---

## CRITICAL Findings

### 1. Authentication Bypass in Upload Endpoints
**File**: `routers/upload.py:27, 53`
**Severity**: CRITICAL

Both `create_upload_session` and `upload_done` accept `request: dict` and extract `user_id` directly from the request body without any authentication dependency:

```python
# routers/upload.py:40-44
user_id = request.get("user_id")
if not user_id:
    raise HTTPException(status_code=400, detail="user_id required")
```

Any caller can forge any `user_id`, gaining upload session and confirmed upload callbacks under arbitrary user accounts.

**Suggested Fix**: Add `user_id: int = Depends(get_current_user_id)` dependency instead of reading from body.

---

### 2. Path Traversal in `_snapshot_workspace`
**File**: `services/fehub_service.py:478-511`
**Severity**: CRITICAL

`dest_key = f"{dest_prefix}{vfs_file_path}"` at line 507 does not sanitize `vfs_file_path` for `../` sequences. A malicious VFS file path like `../../../etc/passwd` would escape the release snapshot directory and overwrite arbitrary COS objects.

Same issue in `_copy_template` at line 145:
```python
dest_key = f"{self.base_path}{target.lstrip('/')}/{rel_path}"
```
`rel_path` comes from COS object keys and is not validated.

**Suggested Fix**: Normalize and validate paths:
```python
from pathlib import PurePosixPath
p = PurePosixPath(vfs_file_path)
if p.is_absolute() or ".." in p.parts:
    return "Error: Invalid path"
dest_key = f"{dest_prefix}{vfs_file_path}"
```

---

## HIGH Findings

### 3. Fire-and-Forget Task Leaks in GroupDispatchService
**File**: `services/group_service.py:75-77, 196, 199-201`
**Severity**: HIGH

`on_message` fires `dispatch_to_members` as a fire-and-forget task, and `agent_reply` recursively fires more dispatches with no await, no task tracking, and no error propagation:

```python
# group_service.py:75-77
asyncio.create_task(
    self.dispatch_to_members(group_id, round=0, exclude=sender_hash)
)
# group_service.py:199-201
asyncio.create_task(
    self.dispatch_to_members(group_id, round=round + 1, exclude=agent_hash)
)
```

If the task raises an exception, it is silently lost. The `_running_tasks` dict partially mitigates this for `agent_reply` (line 117), but the initial dispatch from `on_message` has no tracking.

**Suggested Fix**: Await or track all dispatched tasks; add structured error handling with logging.

---

### 4. Path Traversal in `diff` / `restore`
**File**: `services/fehub_service.py:274-386`
**Severity**: HIGH

`file_path` parameter in `diff()` and `restore()` is passed directly to `vfs.async_read_file()` / `vfs.async_write()` without sanitization. While VFS itself may have some protection, the path is not validated before use.

**Suggested Fix**: Validate `file_path` with `PurePosixPath` — must be relative, no `..` components, within workspace.

---

### 5. In-Memory Session Memory Leak in UploadService
**File**: `services/upload_service.py:41, 73, 88, 105, 114`
**Severity**: HIGH

`UploadService._sessions` is a plain `dict` that grows indefinitely:

1. `create_session` adds sessions (line 73)
2. `confirm_upload` deletes on success (line 105), but only if session found
3. `get_session` deletes expired sessions (line 114)
4. **No background cleanup thread** — if a session expires without being accessed, it remains in `_sessions` forever

A crash mid-session also leaves orphaned entries.

**Suggested Fix**: Add a background cleanup task (like `periodic_share_ref_cleanup` in main.py) that periodically scans for and deletes expired sessions.

---

### 6. Missing `(group_id, created_at)` Composite Index on GroupMessage
**File**: `models/group.py:47-64`
**Severity**: HIGH

`GroupMessage` is queried with `filter(GroupMessage.group_id == group_id).order_by(GroupMessage.created_at.desc()).limit(limit)` in `get_messages()`. The single-column index on `group_id` (line 62) cannot efficiently support the date range ordering. Missing composite index.

**Suggested Fix**:
```python
Index("idx_group_messages_group_created", "group_id", "created_at"),
```

---

### 7. Missing `(group_id, created_at)` Composite Index on GroupMoments
**File**: `models/group.py:67-82`
**Severity**: HIGH

`GroupMoments` has separate single-column indexes but queries filter by `group_id` and order by `created_at`. Missing composite index.

**Suggested Fix**:
```python
Index("idx_group_moments_group_created", "group_id", "created_at"),
```

---

### 8. Unvalidated manifest.json Routes Field
**File**: `services/fehub_service.py:409-415`
**Severity**: HIGH

`manifest` is parsed with `json.loads()` and `routes = manifest.get("routes", [])` is used directly without validating:
- `routes` is not bounded — could be a massive array causing memory issues
- No schema validation for route structure (each route needs `path`, `type`, `file`)
- `app_name` from manifest is used directly in URLs

**Suggested Fix**: Validate manifest structure:
```python
if not isinstance(manifest, dict):
    return "Error: manifest.json must be an object"
routes = manifest.get("routes", [])
if not isinstance(routes, list) or len(routes) > 100:
    return "Error: routes must be a list of ≤100 items"
for r in routes:
    if not isinstance(r, dict) or "path" not in r or "type" not in r:
        return "Error: each route must have path and type"
```

---

## MEDIUM Findings

### 9. Fire-and-Forget WS Push in moments_tools.py
**File**: `services/tools/moments_tools.py:64-73`
**Severity**: MEDIUM

```python
asyncio.create_task(moments_service.push_moments_event(group_id, moment))
```
No await, no error tracking. If WS push fails, the error is logged at debug level and silently discarded. Acceptable for non-critical events, but the implementation has a convoluted event loop check (lines 67-71):

```python
loop = asyncio.get_event_loop()
if loop.is_running():
    asyncio.create_task(...)
else:
    loop.run_until_complete(...)
```

This pattern is fragile — if called from a threadpool context where the loop is not the main loop, behavior is unpredictable.

**Suggested Fix**: Always use `asyncio.create_task` when already in async context; use `asyncio.run()` in a safe thread context wrapper.

---

### 10. N+1 Query in `_format_group`
**File**: `routers/group.py:99-113`
**Severity**: MEDIUM

`_format_group` executes a count query per group:
```python
member_count = db.query(GroupMember).filter(GroupMember.group_id == group.id).count()
```
When listing multiple groups (`list_groups`), this causes N additional queries.

**Suggested Fix**: Use a subquery or join the count in the initial query, or batch-fetch counts.

---

### 11. Thread Safety on UploadService._sessions
**File**: `services/upload_service.py`
**Severity**: MEDIUM

`_sessions` is a plain `dict` accessed from async contexts. While Python's GIL provides some protection, concurrent `create_session` + `confirm_upload` + `get_session` on the same dict from multiple async tasks can cause runtime errors (e.g., `dictionary changed size during iteration`).

**Suggested Fix**: Use `asyncio.Lock` around `_sessions` modifications, or use `collections.abc.Mapping` with thread-safe alternatives.

---

### 12. VFS `list_objects` Without Pagination
**File**: `services/fehub_service.py:636-652`
**Severity**: MEDIUM

`_list_workspace_files` calls `storage.list_objects(cos_prefix)` with no `max_keys` pagination. Large workspaces return all keys in one call, consuming significant memory and network bandwidth.

**Suggested Fix**: Use `max_keys=1000` and paginate if needed (COS `list_objects` supports `Marker` for pagination).

---

### 13. `_get_file_at_commit` Always Returns Current Content
**File**: `services/fehub_service.py:348-365`
**Severity**: MEDIUM

The VCS diff implementation admits in a comment that per-commit file snapshots are not stored. `_get_file_at_commit` falls back to current workspace content:

```python
# Return current content as approximation (not ideal but workable).
current = await self.vfs.async_cat(file_path)
if not current.startswith("Error"):
    return current + "\n[⚠️ 内容为当前版本，commit 时快照未单独存储]"
```

This means `fe vcs diff` between commits is unreliable — it shows current content, not the content at the time of each commit.

**Suggested Fix**: Store file snapshots at commit time (significant change), or at minimum make the warning more prominent and fail explicitly when content differs from commit time.

---

### 14. Mixed Chinese/English Error Messages
**Severity**: MEDIUM

Throughout the codebase, error messages mix Chinese and English:
- `fehub_service.py:79`: `"Error: 目标目录非空且已包含 .fehub，请先备份或切换目录"`
- `fehub_service.py:132`: `"Error: 模板目录不存在或为空: {template_path}"`
- `bash_tools.py:41`: `"Error: 命令 '{cmd_name}' 不在允许的白名单中..."`
- `routers/upload.py:42`: `"user_id required"`

**Suggested Fix**: Establish a language policy. For user-facing messages visible to end users (through web/WeChat), use Chinese. For agent-internal messages and logs, use English consistently.

---

### 15. `_running_tasks` Race Condition
**File**: `services/group_service.py:31, 115-119, 211`
**Severity**: MEDIUM

`_running_tasks` is a plain `dict` accessed from multiple async tasks:
```python
if task_key in self._running_tasks:
    self._running_tasks[task_key].cancel()
self._running_tasks[task_key] = asyncio.create_task(...)
# ...
self._running_tasks.pop(task_key, None)
```

The check-then-act is not atomic — between the `if` and the assignment, another task could cancel or pop the same key. Use an `asyncio.Lock` or `asyncio.TaskGroup` (Python 3.11+).

---

## LOW Findings

### 16. Missing Type Hints
**Files**: Multiple services
**Severity**: LOW

Many methods lack return type hints:
- `services/group_service.py:should_wake` → should return `bool`
- `services/fehub_service.py:_generate_skeleton` → missing return type
- `services/fehub_service.py:_copy_template` → missing return type

**Suggested Fix**: Add type hints per project conventions.

---

### 17. Missing Error Handling in `_call_llm`
**File**: `services/group_service.py:320-340`
**Severity**: LOW

Returns empty string `""` on LLM failure, silently swallowing errors. The caller treats empty response as a valid reply (it gets saved as a `GroupMessage`). Should at minimum log at error level.

---

### 18. `apps_service` Register Called Twice on Publish
**File**: `services/fehub_service.py:426-438`
**Severity**: LOW

```python
register_app_sync(self.agent_hash, app_id)
# ...
register_app_sync(self.agent_hash, app_id)  # called again unconditionally
```
The second call on line 438 is unconditional, even though the first call may have succeeded.

---

### 19. `spawn_subagent` Override in moments_tools.py
**File**: `services/tools/moments_tools.py:118-172`
**Severity**: LOW

The `spawn_subagent` override calls `super().spawn_subagent()` synchronously (line 138) from what may be an async context. If the Mixin is used in an async context, blocking the event loop with a sync call is problematic.

---

### 20. `CosStorage` Instantiated Per-Method in UploadService
**File**: `services/upload_service.py:63, 99`
**Severity**: LOW

```python
storage = CosStorage()  # line 63 and line 99
```
New `CosStorage` instances created on each call. Should reuse a singleton.

---

## Overall Assessment

The Phase 4-8 engine code introduces substantial new functionality with generally sound architecture. The most critical issues are the **authentication bypass in upload endpoints** and the **path traversal vulnerability in `_snapshot_workspace`** — both must be fixed before any production deployment.

The dispatch engine's fire-and-forget task pattern is the most architecturally risky design choice. While it works for low-volume scenarios, it will cause silent message loss under load and makes debugging difficult. Consider switching to a proper task queue (e.g., `asyncio.TaskGroup` with exception observers) once Python 3.11+ is available.

The missing composite database indexes on `GroupMessage` and `GroupMoments` will cause query performance to degrade linearly with message volume — these should be added proactively.

The in-memory `UploadService._sessions` is a ticking memory leak in long-running server processes. A background cleanup task should be added to match the pattern used elsewhere in the codebase.

Code quality is generally good — the code is readable, well-structured, and follows existing project conventions. The main quality gaps are around error handling completeness and the Chinese/English inconsistency in messages.
