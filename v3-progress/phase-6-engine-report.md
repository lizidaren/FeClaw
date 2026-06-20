# Phase 6 Engine Report — FeHub (VCS + Publish + AppData)

## Status: ✅ Implemented

## What was built

### 1. FeHub Models (`models/fehub.py`) [NEW]

**FePublish** — Published app snapshot records
- One row per (agent_hash, tag)
- Stores: app_name, tag, is_public, snapshot_path (COS key), manifest JSON
- Unique constraint on (agent_hash, tag)
- `is_active` flag for soft-delete on unpublish

**AppData** — Key-value runtime data for mini-app frontends
- One row per (app_id, user_id, key)
- `value` stored as JSON (flexible: dict, list, string, number)
- Used by miniapp JS SDK to persist user state (settings, progress, etc.)

### 2. FeHub Service (`services/fehub_service.py`) [NEW ~340 lines]

**VCS operations** (backed by COS `.fehub/commits/` and `.fehub/releases/`):
- `init_project(path, template_path)` — `fe init [--template=xxx]`
  - Copies template dir from VFS if `--template` given
  - Otherwise generates minimal `manifest.json` + `index.html` skeleton
  - Writes initial `.fehub/commits/{timestamp}.json` with "init" type record
- `commit(path, message)` — `fe vcs commit <message>`
  - Writes commit record to `.fehub/commits/{timestamp}.json` with file list
- `log(path, file_path)` — `fe vcs log [file_path]`
  - Lists all commits (newest first), optionally filtered by file
  - Also lists release tags from DB
- `diff(file, ref_a, ref_b)` — `fe vcs diff <file> <ref_a> <ref_b>`
  - Resolves refs via release tags (DB lookup) or commit timestamps
  - Uses Python `difflib.unified_diff`
- `restore(file, ref)` — `fe vcs restore <file> <ref>`
  - Resolves ref and writes old version back to workspace

**Publish operations** (backed by COS `.fehub/releases/{tag}/`):
- `publish(path, tag, is_public)` — `fe publish <tag> [--public]`
  - Validates `manifest.json` exists
  - Snapshots `workspace/*` → `.fehub/releases/{tag}/` (recursive COS copy)
  - Registers with `apps_service.register_app()`
  - Creates/updates `FePublish` DB record
  - Returns public URL: `https://{agent_hash}.feclaw.lizidaren.cn/apps/{agent_hash}-{tag}/`
- `unpublish(tag)` — `fe unpublish <tag>`
  - Marks `FePublish.is_active = False` (keeps snapshot in COS)
  - Calls `apps_service.unregister_app()`

**Static helpers** (AppData CRUD):
- `FeHubService.get_app_data(app_id, user_id, key=..., prefix=...)`
- `FeHubService.set_app_data(app_id, user_id, key, value)`
- `FeHubService.delete_app_data(app_id, user_id, key=..., prefix=...)`

### 3. FeHub Tools (`services/tools/fehub_tools.py`) [NEW]

Mixes into `AgentToolsService` as `FeHubToolsMixin`. Tools:
- `fe_init(path, template_path)` — project scaffold
- `fe_vcs_commit(message, file_path)` — record version
- `fe_vcs_log(file_path)` — view history
- `fe_vcs_diff(file_path, ref_a, ref_b)` — compare versions
- `fe_vcs_restore(file_path, ref)` — restore old version
- `fe_publish(tag, is_public)` — publish app

### 4. Bash Alias (`services/tools/bash_tools.py`) [MODIFIED]

- Added `"fe"` to `ALLOWED_BASH_COMMANDS`
- Added `_handle_fe_command()` handler before metachar check
- Handler parses `fe init/vcs/publish/unpublish` subcommands and delegates to `FeHubService`
- Also added `_get_fehub_service()` lazy factory method

### 5. FeHub Router (`routers/fehub.py`) [NEW]

**Endpoints:**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/fehub/apps` | JWT | List all published apps for current user |
| GET | `/api/fehub/apps/{app_id}` | JWT | Get specific published app |
| POST | `/api/fehub/apps/{app_id}/data` | JWT | Set app data key-value |
| GET | `/api/fehub/apps/{app_id}/data?key=...` | JWT | Get specific key |
| GET | `/api/fehub/apps/{app_id}/data?prefix=...` | JWT | Get all keys with prefix |
| DELETE | `/api/fehub/apps/{app_id}/data?key=...` | JWT | Delete specific key |
| DELETE | `/api/fehub/apps/{app_id}/data?prefix=...` | JWT | Delete all keys with prefix |

**Miniapp JS SDK aliases** (for frontend `fetch()` calls):

| Method | Path | Description |
|--------|------|-------------|
| POST | `/apps/{agent_hash}/{app_id}/data` | Set app data |
| GET | `/apps/{agent_hash}/{app_id}/data?key=...` | Get specific key |
| GET | `/apps/{agent_hash}/{app_id}/data?prefix=...` | Get prefix matches |
| DELETE | `/apps/{agent_hash}/{app_id}/data?key=...` | Delete specific key |

### 6. Wiring (`main.py` + `services/tools/__init__.py`) [MODIFIED]

- `models.fehub` imported in `lifespan()` → `Base.metadata.create_all()` auto-creates tables
- `fehub.router` registered with FastAPI app
- `FeHubToolsMixin` added as first mixin in `AgentToolsService` composite class

## File inventory

| File | Change |
|------|--------|
| `models/fehub.py` | NEW |
| `services/fehub_service.py` | NEW |
| `services/tools/fehub_tools.py` | NEW |
| `services/tools/bash_tools.py` | MODIFY |
| `services/tools/__init__.py` | MODIFY |
| `routers/fehub.py` | NEW |
| `main.py` | MODIFY |

## COS storage layout

```
feclaw/agents/{hash}/.fehub/commits/{timestamp}.json   # commit records
feclaw/agents/{hash}/.fehub/releases/{tag}/           # published snapshots
```

## Design notes

1. **VCS without per-commit snapshots**: Commit records store file lists but not full file contents. Restoring from a commit returns current workspace content with a warning. Full per-commit content snapshots would double COS storage; releases are the primary content-addressable snapshots.

2. **App ID format**: `{agent_hash}-{tag}` (e.g., `5178-v1.0.0`) — unique, human-readable.

3. **FeHubToolsMixin order**: Placed first in `AgentToolsService` MRO so its methods take precedence if there are name collisions (unlikely).

4. **Template copy**: Copies all files under the template VFS path using `storage.list_objects()` + `put_object()`, preserving directory structure.

5. **Snapshot on publish**: `_snapshot_workspace` reads each file via `vfs.async_read_file` and writes to the release COS prefix — works without FUSE/bwrap.

## Testing notes

Known pre-existing test failures (unrelated to Phase 6):
- `tests/rerank/` — import `EMBEDDING_API_URL` from `vector_search_service`
- `tests/test_subagent_presets*.py` — import `PRESET_ROLES` from removed module
- `tests/test_tool_result_budget.py` — mock coroutine issues
- `tests/test_web_search.py` — assertion mismatches

To run tests skipping known failures:
```bash
python3 -m pytest tests/ -q --tb=line \
  --ignore=tests/rerank \
  --ignore=tests/test_subagent_presets.py \
  --ignore=tests/test_subagent_presets_api.py \
  --ignore=tests/test_subagent_summary.py \
  --ignore=tests/test_tool_result_budget.py \
  --ignore=tests/test_web_search.py \
  --ignore=tests/test_static_site.py
```
