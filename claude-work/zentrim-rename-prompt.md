# Zentrim Rename — Curio → Zentrim (格物所)

## Context

FeClaw's Curio module is being renamed to "Zentrim" (格物所 in Chinese). Zero production data in old tables. Full rename is safe.

## Task: Rename all "curio" → "zentrim" in the following files

### Step 1: File renames (use `git mv`)
- `models/curio.py` → `models/zentrim.py`
- `services/curio_service.py` → `services/zentrim_service.py`
- `routers/curio.py` → `routers/zentrim.py`
- `docs/v1/02-curio.md` → `docs/v1/02-zentrim.md`
- `docs/v1/02-curio-canvas-impl.md` → `docs/v1/02-zentrim-canvas-impl.md`

### Step 2: models/zentrim.py
Rename:
- `CurioEntry` → `ZentrimEntry`
- `CurioTimeline` → `ZentrimTimeline`
- `CurioTimelineEntry` → `ZentrimTimelineEntry`
- `CurioReference` → `ZentrimReference`

Table names (`__tablename__`):
- `curio_entries` → `zentrim_entries`
- `curio_timelines` → `zentrim_timelines`
- `curio_timeline_entries` → `zentrim_timeline_entries`
- `curio_references` → `zentrim_references`

Index/constraint names:
- `idx_curio_entries_user_created` → `idx_zentrim_entries_user_created`
- `idx_curio_entries_user_type` → `idx_zentrim_entries_user_type`
- `idx_curio_entries_status` → `idx_zentrim_entries_status`
- `ck_curio_entries_type` → `ck_zentrim_entries_type`
- `ck_curio_entries_status` → `ck_zentrim_entries_status`
- `idx_curio_timelines_user` → `idx_zentrim_timelines_user`
- `idx_curio_timeline_entries_timeline` → `idx_zentrim_timeline_entries_timeline`
- `idx_curio_timeline_entries_entry` → `idx_zentrim_timeline_entries_entry`
- `idx_curio_references_source` → `idx_zentrim_references_source`
- `idx_curio_references_target` → `idx_zentrim_references_target`

Update docstrings: `Curio（格物所）` → `Zentrim（格物所）`

### Step 3: services/zentrim_service.py
- All references to `CurioEntry` → `ZentrimEntry`
- All references to `CurioTimeline` → `ZentrimTimeline`
- All references to `CurioTimelineEntry` → `ZentrimTimelineEntry`
- All references to `CurioReference` → `ZentrimReference`
- `class CurioService` → `class ZentrimService`
- Docstring: `Curio（格物所）业务服务` → `Zentrim（格物所）业务服务`
- import: `from models.curio import ...` → `from models.zentrim import ...`
- Log prefix: `[CurioService]` → `[ZentrimService]`
- COS path: `curio/user_{uid}/attachments/...` → `zentrim/user_{uid}/attachments/...`
- Vector index: `idx-curio-{uid}` → `idx-zentrim-{uid}`
- All method docstrings that say "Curio"

### Step 4: routers/zentrim.py
- import: `from services.curio_service import CurioService, _generate_ulid` → `from services.zentrim_service import ZentrimService, _generate_ulid`
- Router: `prefix="/api/curio", tags=["Curio"]` → `prefix="/api/zentrim", tags=["Zentrim"]`
- All route paths: `/api/curio/*` → `/api/zentrim/*`
- All `CurioService.xxx()` → `ZentrimService.xxx()`
- Log prefix: `[Curio]` → `[Zentrim]`
- Audit logs: `[audit] curio.*` → `[audit] zentrim.*`
- Docstring comments: `Curio（格物所）` → `Zentrim（格物所）`

### Step 5: main.py (3 lines)
```
line 72:  from routers.curio import router as curio_router
          → from routers.zentrim import router as zentrim_router
line 92:  from models.curio import CurioEntry, CurioTimeline, CurioTimelineEntry, CurioReference
          → from models.zentrim import ZentrimEntry, ZentrimTimeline, ZentrimTimelineEntry, ZentrimReference
line 394: app.include_router(curio_router)
          → app.include_router(zentrim_router)
```

### Step 6: models/__init__.py (5 lines)
```
# Curio（格物所）models → # Zentrim（格物所）models
from models.curio import (  → from models.zentrim import (
    CurioEntry,   → ZentrimEntry,
    CurioTimeline → ZentrimTimeline,
    CurioTimelineEntry → ZentrimTimelineEntry,
    CurioReference, → ZentrimReference,
```

### Step 7: docs/v1/ files
- `02-curio.md` → Update all "Curio" references to "Zentrim" / "格物所"
- `02-curio-canvas-impl.md` → Same
- `docs/v1/index.md` → Update reference paths from curio to zentrim

### Step 8: Drop old database tables
Write the SQL to a file `scripts/drop_curio_tables.sql`:
```sql
DROP TABLE IF EXISTS curio_references;
DROP TABLE IF EXISTS curio_timeline_entries;
DROP TABLE IF EXISTS curio_timelines;
DROP TABLE IF EXISTS curio_entries;
```

## Verification
After all changes, run: `python3 -m py_compile main.py && python3 -c "from models.zentrim import *; print('OK')"`

## IMPORTANT
- Do NOT touch claude-logs/, claude-audit/, history/ directories
- Do NOT touch .gitignore
- Use `git mv` for file renames so git history is preserved
- After each step, verify the changes look correct
