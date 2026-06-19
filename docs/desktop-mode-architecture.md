# FeClaw Desktop Mode 架构设计

> 版本 1 — 2026-06-19
> 在 Server + Desktop 兼容性讨论后的总方案

---

## 1. 核心原则

> **FeClaw 永远是服务器。** 它在 `127.0.0.1:PORT` 提供全部 API。
> FeClaw-Desktop 只是一个更漂亮的浏览器——零处理能力，纯 GUI。

```
┌──────────────────────┐     HTTP/REST      ┌──────────────────┐
│  FeClaw-Desktop      │ ──────────────────→│  FeClaw 引擎     │
│  (原生 GUI Shell)    │ ←─────────────────│  (智能体平台)    │
│                      │    JSON            │                   │
│  - 系统托盘           │                    │  - LLM 调用       │
│  - 原生弹窗           │                    │  - 工具执行       │
│  - 开机自启           │                    │  - 知识库         │
│  - WeChat 联动       │                    │  - 错题识别       │
│  - 更新管理           │                    │  - WeChat 通道    │
│  - 零处理能力         │                    │  - 存储后端       │
└──────────────────────┘                    └──────────────────┘
```

### 1.1 分界线

| FeClaw 引擎（永不改） | FeClaw-Desktop（轻量壳） |
|----------------------|------------------------|
| LLM 调用、工具执行 | 启动/停止引擎 |
| 知识库检索 | 系统托盘图标 |
| 错题识别 Pipeline | 原生弹窗确认 |
| WeChat 消息通道 | 开机自启注册 |
| 用户认证（TOTP/OAuth） | 更新检查 |
| 存储后端（COS/Local） | 状态指示器 |

### 1.2 "Desktop 模式"是什么

FeClaw 引擎本身**不需要** `if MODE == "desktop"` 条件分支。它就是一个完整的服务器，在有 `STORAGE_MODE=local` 配置时可以单机运行。

所谓 "Desktop 模式" 实际上只是：

1. **配置不同**: `STORAGE_MODE=local`, `AUTH_DISABLE_LOCAL=true`（可选）
2. **启动方式不同**: `feclaw --port 8080` 而非 systemd
3. **数据目录不同**: `~/.feclaw/` 而非 `/mnt/feclaw/`

引擎不自知自己是 Desktop 还是 Server——它只是按配置运行。

---

## 2. Desktop 与 Server 的差异点

### 2.1 必须不同的（有抽象层隔离）

| 维度 | Server | Desktop | 状态 |
|------|--------|---------|------|
| 文件存储 | `CosStorage`（腾讯云 COS） | `LocalStorage`（`~/.feclaw/data/`） | ✅ 已实现 |
| 向量搜索 | `CosVectorStorage` | `SqliteVecStorage` / `NumpyVecStorage` | ✅ 已实现 |
| 代码执行 | bwrap 沙箱 + seccomp | subprocess + Web 弹窗确认 | 🔄 待设计 |
| 用户认证 | OAuth + TOTP（完整版） | 首次启动自建 admin，可选关 auth | 🔄 待实现 |
| 前端文件上传 | 预签名 URL 直传 COS | POST body → 后端写本地 | 🔄 待实现 |
| 前端 CDN | COS 静态网站托管 | `feclaw serve-static` 内置 | 🔄 待实现 |

### 2.2 完全相同（共享代码，零改动）

| 模块 | 说明 |
|------|------|
| LLM 调用 (llm_service.py) | 调 API，跨平台 |
| 工具注册 (tool_registry.py) | 工具定义，跨平台 |
| ChatService (chat_service.py) | 聊天逻辑，跨平台 |
| SmartRouter | 模型路由，跨平台 |
| Session Memory | 对话历史管理，跨平台 |
| WeChat 消息通道 | HTTP API 通信，跨平台 |
| Cron 定时任务 | APScheduler，跨平台 |
| TOTP 认证 | QR 码 + 一次性密码，跨平台 |
| Admin 面板 | Web UI，跨浏览器 |
| **全部 36+ Agent 工具** | 纯 Python，跨平台 |

### 2.3 Desktop 用户不应察觉的差异

Desktop 用户看到的：

```
✅ 聊天 / 知识库 / 错题识别 → 跟 Server 一模一样
✅ Web UI / Agent 配置 / Prompt 编辑 → 一模一样
✅ WeChat 连接 / 定时任务 / 多 Agent → 一模一样
✅ TOTP 登录（密码首次终端打印）→ 一样的流程

❌ 不能：注册新用户（单用户系统）
❌ 不能：ssh 远程访问（仅 127.0.0.1）
❌ 不需要：配 COS 密钥 / 域名 / SSL
```

---

## 3. 引擎改动（最小化）

### 3.1 config.py

```python
# 现有字段
STORAGE_MODE: str = "cos"         # 默认 cos，无配置时须显式改为 local
LOCAL_STORAGE_ROOT: str = ""      # 空 = ~/.feclaw/data/

# 新增字段（可选）
AUTH_DISABLE_LOCAL: bool = False  # Desktop 场景可关 auth
BIND_HOST: str = "0.0.0.0"       # Desktop 固定 127.0.0.1
```

### 3.2 数据目录规范

```
~/.feclaw/
├── data/              ← LOCAL_STORAGE_ROOT（文件存储）
├── feclaw.db          ← SQLite 数据库
├── vectors/           ← 向量存储数据
├── config.toml        ← 用户可编辑配置
├── logs/              ← 运行日志
└── .first_run         ← 哨兵文件（判断是否首次启动）
```

首次启动流程：
1. 检测 `~/.feclaw/.first_run` 不存在
2. 自动创建 admin 账号，密码打印到终端
3. 写入 `.first_run` 哨兵
4. 启动 Web UI，提示登录

### 3.3 不需要改的文件

```python
# 禁止引入条件判断的文件：
main.py             # ← 已经靠 Config 配置驱动，不感知 mode
all routers/*.py    # ← 路由全部注册，auth 中间件只拦无 token 请求
all tools/*.py      # ← 工具定义无平台依赖
services/llm_service.py
services/chat_service.py
services/session_memory_service.py
services/tool_registry.py
```

---

## 4. FeClaw-Desktop 客户端

### 4.1 技术选型

| 方案 | 体积 | 难度 | 推荐 |
|------|------|------|------|
| **Tauri** (Rust + WebView) | <10MB | 🟡 需 Rust | ⭐ 最推荐 |
| pywebview (Python) | +~5MB | 🟢 零新语言 | 速度最快 |
| Electron (Node.js) | 100MB+ | 🟢 生态好 | 太重 |
| C# WinUI 3 | 需 .NET | 🔴 跨平台难 | Windows only |

**推荐 Tauri 2.0**：单个 exe <10MB，原生 WebView 渲染，Rust 后端能做系统托盘、原生弹窗、开机自启、进程管理。

### 4.2 Tauri 客户端功能清单

| 功能 | 实现方式 |
|------|---------|
| 引擎生命周期管理 | Rust 侧 `std::process::Command` spawn/kill FeClaw |
| 系统托盘 | `tray-icon` crate，右键菜单：打开/退出/重启 |
| 原生弹窗确认 | Rust 侧 `rfd` crate (`MessageDialog`) |
| 开机自启 | `auto-launch` crate |
| Web UI 渲染 | Tauri WebView，指向 `http://127.0.0.1:8080` |
| 更新检查 | GitHub Releases API 版本比对 |
| 状态指示器 | 托盘图标颜色变化（绿=运行，灰=停止，红=错误） |

### 4.3 核心流程（伪代码）

```rust
// main.rs — Tauri 入口
fn main() {
    // 1. 启动 FeClaw 引擎
    let engine = Command::new("feclaw")
        .args(["--port", "8080", "--host", "127.0.0.1"])
        .spawn();

    // 2. 等待引擎就绪（轮询 127.0.0.1:8080/health）
    wait_for_engine("http://127.0.0.1:8080/health");

    // 3. 打开原生窗口（WebView 加载引擎 Web UI）
    tauri::Builder::default()
        .system_tray(tray_menu())
        .on_system_tray_event(handle_tray_event)
        .invoke_handler(tauri::generate_handler![
            show_confirm_dialog,  // 弹窗确认
            get_engine_status,    // 引擎状态
            open_in_browser,      // 外部浏览器打开
        ])
        .run(tauri::generate_context!());
}
```

### 4.4 不 pywebview 的原由

pywebview 不用学新语言，但它的问题是：
- **没有系统托盘标准支持**（可以 hack，但不稳定）
- **打包体积大**（PyInstaller 打包 Python 解释器）
- **进程管理麻烦**（spawn 引擎进程后，退出时清理是噩梦）
- **弹窗确认**：只能用 `ctypes.windll.user32.MessageBoxW`，复杂交互做不了

Tauri 虽然要写几行 Rust，但这些问题全是它的一等公民功能。

---

## 5. 代码执行确认（弹窗确认机制）

### 5.1 架构

```
Agent 想执行: rm -rf C:\Windows

  → bash_tool.py 检测 mode
      → Desktop: 不执行，改为走 ConsentManager
          → POST /api/sandbox/consent-request { command, risk_level }
              → 桌面客户端轮询 /api/sandbox/consent-pending
                  → Tauri Rust 侧弹原生窗
                      → 用户点"允许"/"拒绝"/"始终允许"
                  → POST /api/sandbox/consent-response { id, decision }
                      → bash_tool.py 继续/终止
```

### 5.2 风险等级

| 等级 | 操作 | 行为 |
|------|------|------|
| L1 | 读文件 (cat, ls, grep) | 静默执行，不弹窗 |
| L2 | 写文件 (echo >, cp, mv) | Web 弹窗确认 |
| L3 | 删文件 (rm, del) | 原生系统级弹窗 + 红色警告 |
| L4 | 网络 (curl, wget) | Web 弹窗确认 |
| L5 | Python 代码执行 | Web 弹窗确认 |
| skip | 用户已标记"始终信任" | 免打扰直行 |

### 5.3 前端无 WebSocket 时的降级

如果 Desktop 关闭了浏览器窗口，但 WeChat 发来了消息：

```
Agent 用 WeChat 通道回复：
"🛡️ 我需要执行：rm file.txt，回复 Y 确认 / N 拒绝"
用户微信回复 Y
Agent 继续执行
```

这个机制复用 WeChat 通道，**不需要写任何额外代码**。

---

## 6. 实施路线图

### Phase 1: 引擎就绪（当前，1-2 天）
- [x] FileStorage 抽象层实现
- [x] LocalStorage 实现
- [x] 向量搜索 SqliteVec/Numpy 实现
- [ ] config.py 默认值优化（`STORAGE_MODE=local` + `~/.feclaw/` 目录规范）
- [ ] `create_file_storage("auto")` 改为显式模式选择

### Phase 2: Desktop 客户端原型（Tauri，3-5 天）
- [ ] Tauri 项目脚手架
- [ ] FeClaw 引擎进程管理（spawn/kill/health check）
- [ ] 系统托盘菜单
- [ ] WebView 内嵌引擎 UI
- [ ] 首次启动引导流程

### Phase 3: 弹窗确认（2-3 天）
- [ ] 引擎端 ConsentManager 服务
- [ ] Tauri 端 `show_confirm_dialog` 命令
- [ ] 风险等级分类逻辑
- [ ] WeChat 降级确认（复用已有通道）

### Phase 4: 成熟化（持续）
- [ ] 开机自启注册
- [ ] 自动更新
- [ ] 日志查看器
- [ ] Windows MSI 安装包

---

## 7. 不变的承诺

| 承诺 | 原因 |
|------|------|
| FeClaw 引擎不会为 Desktop 加条件分支 | Server 稳定性第一 |
| FeClaw-Desktop 零 AI 处理能力 | 纯 GUI shell，不重复造轮子 |
| Desktop 用户功能不能少于 Server 用户 | WeChat、Cron、多 Agent、Admin 全保留 |
| Desktop 用户无需读文档就能用 | 首次启动自建用户、自动数据目录 |
| 所有交互入口可换 | 引擎 API + Desktop GUI 松耦合 |
