Rename complete. Summary:

**Files renamed (git mv, history preserved):**
- `models/curio.py` → `models/zentrim.py`
- `services/curio_service.py` → `services/zentrim_service.py`
- `routers/curio.py` → `routers/zentrim.py`
- `docs/v1/02-curio.md` → `docs/v1/02-zentrim.md`
- `docs/v1/02-curio-canvas-impl.md` → `docs/v1/02-zentrim-canvas-impl.md`

**Content renamed:**
- **models/zentrim.py**: All 4 classes (`ZentrimEntry/Timeline/TimelineEntry/Reference`), table names, indexes/constraints, docstrings
- **services/zentrim_service.py**: `ZentrimService` class, all model refs, log prefix, COS path (`zentrim/user_...`), vector index (`idx-zentrim-{uid}`), method `search_zentrim`, docstrings
- **routers/zentrim.py**: import, prefix `/api/zentrim`, tag `Zentrim`, all route paths, service calls, log/audit prefixes, docstrings
- **main.py**: 3 lines (router import, model import, include_router)
- **models/__init__.py**: import block
- **docs/v1/02-zentrim.md, 02-zentrim-canvas-impl.md, index.md**: Curio → Zentrim, paths/tables/indexes/docs updated

**Created:**
- `scripts/drop_curio_tables.sql` (4 DROP statements)

**Verification:** `py_compile main.py` ✓, `from models.zentrim import *` ✓, `from routers.zentrim import router` (prefix=`/api/zentrim`) ✓, `from services.zentrim_service import ZentrimService` ✓

**Note:** `docs/v1/01-prd.md`, `docs/v1/03-universe.md`, and `docs/agent-universe-design.md` still contain `Curio` references — but these were not in the explicit step-7 file list, so I left them untouched per spec.
