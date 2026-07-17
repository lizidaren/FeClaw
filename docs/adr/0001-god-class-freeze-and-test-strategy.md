# ADR 0001: God Class Freeze & Test Infrastructure Hardening

- **Status**: Accepted
- **Date**: 2026-07-16
- **Deciders**: FeClaw maintainer
- **Related**: `claude-logs/0716-architecture-review.md`, `FirstEntranceDocs/02_FECLAW/key_patterns.md`

---

## Context

FeClaw 的 0716 架构审查识别出若干正在恶化的演进伤口：

- **God class 涌现**：`virtual_filesystem.py`（3178 行）、`wechat_service.py`（2006 行）、`chat_service.py`（1633 行）
- **跨切面关注点散落**：认证在 `utils/auth.py` + 多个 router 各写一份；`get_db` 有 3 份实现
- **数据模型漂移**：`agent_hash` 列宽 `String(4)`/`String(16)`/`String(32)`/`String(64)` 不一致
- **并发/可观测性缺陷**：`LLMProvider.last_usage` 跨实例读取，可变全局状态
- **测试失真**：`tests/conftest.py:32` `MagicMock(SessionLocal)` 替真 DB
- **已知 TODO**：`utils/auth.py:33` SHA-256 密码哈希未迁移 bcrypt

历史背景：`FirstEntranceDocs/06_REFERENCES/internal_docs.md:14` 引用了 `docs/refactor_master_plan.md`，但该文件从未存在 —— 这是文档漂移的活化石。

---

## Decision

本 ADR 确立以下三条核心决策，**适用于 FeClaw 所有后续重构**：

### D1: 冻结 God Class 增长（Phase 2 拆分前置条件）

**禁**：在以下文件中新增非本职责的代码块
- `services/virtual_filesystem.py` —— 本轮仅做 P1.2 (`cos_key_for` 出口)
- `services/wechat_service.py` —— 本轮完全不动
- `services/chat_service.py` —— 本轮完全不动

**允**：上述文件内部对既有职责的修复（如 P0.5 `last_usage`）

**完整拆分推迟到 Phase 2**（Week 4+），前置条件：
- P1.4 真实 DB fixture 上线（测试基础设施就绪）
- P1.2 VFSPath 类型层约束落地（拆分边界清晰）

### D2: 收紧跨切面关注点

所有路由统一从 `utils/auth_dependencies.py` 取认证依赖；所有路由统一从 `models.database.get_db` 取 DB session。本轮完成收拢，下轮禁止在路由内重定义。

### D3: 测试基础设施双轨制

- **默认继续用 `mock_db`**（向后兼容，不破 CI）
- **新增 opt-in `real_db` fixture**（SQLite in-memory，零基础设施）逐步替换关键路径
- **CI 标记策略**：`pytest -m "not real_db"` 全跑；`real_db` 用例仅 main 分支必跑

---

## Scope（10 项可执行工作）

| ID | 项 | 估时 | 状态 |
|----|----|------|------|
| **P0.1** | 创建本 ADR + 修正 `internal_docs.md` 文档漂移 | 0.5d | ✅ 已落地 |
| **P0.2** | 认证依赖统一：建 `utils/auth_dependencies.py`（仅合并全局 HS256 本地 JWT 重复，TOTP 保留独立） | 1d | ✅ 已落地 |
| **P0.3** | 三份 `get_db()` 收归一份 | 0.25d | ✅ 已落地 |
| **P0.4** | SHA-256 → bcrypt 透明懒迁移 | 1.5d | ✅ 已落地 |
| **P0.5** | `LLMProvider.last_usage` 并发 bug 修复 | 1d | ✅ 已落地 |
| **P1.1** | `CONTEXT_LIMIT` 等魔法数字进 `config.Limits` | 0.5d | ✅ 已落地 |
| **P1.2** | VFSPath 类型层约束（Golden Rule 自动化） | 2d | ✅ 已落地 |
| **P1.3** | `agent_hash` 列宽统一 `String(8)`（老数据不动） | 1.5d | ✅ 已落地 |
| **P1.4** | 测试 `real_db` fixture + 关键用例迁移 | 2d | ✅ 已落地 |
| **P1.5** | 最小 metrics endpoint（admin-only） | 1d | ✅ 已落地 |

**Phase 0 总计**：~5 engineer-days（Week 1）
**Phase 1 总计**：~10 engineer-days（Weeks 2-3）

---

## Non-Goals（本轮明确不做）

- ❌ **VFS God Class 完整拆分**（3178 → 多文件）—— 需契约测试先行，约 6 周
- ❌ **WeChat iLink 服务内部拆分** —— 竞争壁垒，无 E2E 测试前不动
- ❌ **Telegram / Discord 多平台通道** —— Phase 3 路线图
- ❌ **FK cascade 全量补齐** —— 需数据审计 + 停机窗口
- ❌ **OpenTelemetry / Prometheus 全栈引入** —— 本轮只做最小 metrics

---

## Architectural Invariants（必须保留）

| 不变量 | 来源 |
|--------|------|
| Golden Rule：VFS `/` ↔ COS `agents/{hash}{vfs_path}` 拼接禁止 `workspace` 段重复 | `key_patterns.md` |
| Channel-agnostic core（`ChatService` 复用） | `design_philosophy.md` |
| Dual-write（`WeChatMessage` + `ChatHistory`） | `key_patterns.md` |
| SSE 事件契约（`thinking`/`tool`/`tool_result`/`message`/`done`） | `key_patterns.md` |
| OAuth 流程完整性（Platform Provider, FeClaw Client, JWKS） | `oauth_service.md` |
| DB 列级迁移（SQLAlchemy `create_all()` 不修改既有表，需 lifespan `ALTER TABLE`） | `database.md` |
| SmartRouter 预决策层 | `smart_router.md` |
| `@tool` 装饰器注册模式 | `tool_registry.md` |
| `agent_hash` 历史值不动（如 `5656`、`8d85`）—— 子域名 URL 向后兼容 | 用户明确要求 |

---

## Verification Strategy

每个 PR 合并前：
1. `pytest tests/ -v`（44 文件全套）
2. `python -c "from main import app"` 导入烟测
3. `uvicorn main:app --port 8080 &` + `curl /health` 200

每项 PR 专项验证详见 `/home/lch/.claude/plans/splendid-sleeping-dawn.md` §验证计划。

---

## Deployment Discipline

参考 `FirstEntranceDocs/04_DEPLOYMENT/troubleshooting.md`：
- **两阶段部署**：`139.199.89.129` staging tag 观察 24h → prod
- **回滚护栏**：每个 PR 保留旧路径加 feature flag 7 天
- **强制 ADR 引用**：每个 PR 描述里必须 cite 本 ADR 或未来 ADR 编号

---

## ADR Discipline（自身使用规则）

后续涉及架构决策的变更，必须：
1. 新建 `docs/adr/NNNN-<title>.md`（不可变历史记录）
2. 更新本文档 Scope 表跟踪状态
3. CI 可在 grep 层校验"重大变更是否引用 ADR"

---

## Consequences

**正面**：
- God class 增长被冻结，给 Phase 2 拆分留出干净起点
- 跨切面关注点收拢后，路由内业务逻辑清晰度提升
- 测试基础设施双轨制避免一次性破 CI
- ADR 纪律让架构决策可追溯

**负面 / 风险**：
- `real_db` fixture 落地需要测试作者自觉采用，不会自动覆盖所有用例
- bcrypt 迁移老用户的 `password_version` 字段长期遗留（约 90 天后才可清理 `salt` 列）
- `last_usage` 字段作为 deprecation 兜底保留 2 周，期间需持续 grep 引用
- 本 ADR 一旦签发即为不可变历史，未来推翻需要新建 ADR 引用本 ADR