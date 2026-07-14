# FeClaw v1.0.0 文档索引

> 最后更新：2026-07-10
> 版本：v1.0.0
> 状态：初稿

---

## 文档间关系

```
             FeClaw v1.0.0
                  │
         ┌────────┼────────┐
         │        │        │
        PRD      Zentrim   Universe
      (总纲)    (详细)    (详细)
         │        │
         └────────┼────────┐
                  │        │
               Agent     Desktop
              引擎设计    架构设计
```

| 文档 | 位置 | 状态 | 说明 |
|:-----|:-----|:----:|:-----|
| **01-prd.md** | `docs/v1/01-prd.md` | 📝 待定稿 | 产品需求总纲。谁在用 FeClaw、为什么、长什么样、三端分工、Build Plan。**唯一 SoT**，覆盖 Universe 白皮书和 Agent-V2 设计中的产品层内容。 |
| **02-zentrim.md** | `docs/v1/02-zentrim.md` | 📝 本篇 | Zentrim（格物所）完整设计。主页/时间线/注意到/画布/拍照入库/搜索/数据模型/Pipeline。 |
| **03-universe.md** | `docs/v1/03-universe.md` | 📝 已移入 | Universe PBL 世界设计。种子系统/NPC/场景/地形系统(区块模型)/版本管理/发布流程。移入自 `agent-universe-design.md`。 |
| **04-engine.md** | `docs/v1/04-engine.md` | ⏳ 待移入 | Agent 引擎核心设计。IRQ/WorkSession/Buffer+Flush/协处理器。移入自 `agent-v2-daemon-design.md`。 |
| **05-desktop.md** | `docs/v1/05-desktop.md` | ⏳ 待移入 | Desktop 架构设计。Tauri 壳/双模式/WS 协议/弹窗确认。移入自 `desktop-mode-architecture.md`。 |
| **06-tdd.md** | `docs/v1/06-tdd.md` | 📝 待定稿 | 技术设计总纲。存储/API/渠道/搜索/扩展性/风险表。 |

## 版本历史

| 版本 | 日期 | 变更 |
|:----|:----|:-----|
| v0.x | 2026-06~ | Agent Harness 底层能力开发阶段。无统一产品文档。 |
| **v1.0.0** | **2026-07-10** | **首次有统一版本的产品级发布。Zentrim + Universe 以独立产品形态出现。完整产品哲学与设计原则。** |

## 版本号规则

所有 FeClaw 项目共享同一版本号：
- `FeClaw/`（引擎）、`FeClaw-Desktop/`、`FeClaw-Mobile/` 全部标为 v1.0.0
- major 版本变动（架构/定位大改）才升级主版本
- minor 版本对应功能发布（如 Zentrim 画布上线 → v1.1.0）
- patch 对应小修小补

## 文档原则

1. **PRD 是唯一事实源（source of truth）。** 如果其他设计文档与 PRD 冲突，以 PRD 为准。
2. **设计文档只写「为什么 + 怎么做」，不重复 PRD 的「是什么」。**
3. **每份文档头带版本声明和 supersedes 字段。**
