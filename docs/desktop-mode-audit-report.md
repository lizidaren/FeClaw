# FeClaw Desktop 架构设计文档 — 代码审阅报告

> 审阅对象: `docs/desktop-mode-architecture.md` (v1, 2026-06-19)
> 审阅员: 资深 Python 全栈架构评审
> 审阅日期: 2026-06-19
> 范围: 文档方案 vs. FeClaw 现有代码库

---

## 摘要

| 维度 | 评级 |
|------|------|
| 整体架构思路（引擎 vs. 壳解耦） | ✅ 方向正确 |
| 声称"引擎零改动" | ⚠️ 多个 P0/P1 隐含改动 |
| `config.py` 改动 | 🔴 存在事实错误 |
| `~/.feclaw/` 目录规范 | 🟡 现有代码不一致 |
| ConsentManager 架构 | ✅ 可行，但有简化空间 |
| Tauri 客户端选型 | 🟡 合理但 pywebview 不应被否 |
| Phase 1-4 时间估算 | 🔴 严重低估 |

**P0 关键问题 3 项 · P1 改进建议 8 项 · Q 待决问题 5 项 · ✅ 验证通过 4 项**

---

## 1. 核心原则验证

### 1.1 "引擎不需要 `if MODE == "desktop"`" — ⚠️ **P1**

**文档原话**: "FeClaw 引擎本身**不需要** `if MODE == "desktop"` 条件分支。"

**代码验证**:

✅ **架构层面成立** — FastAPI 路由、Agent 工具、ChatService 等与 platform 无关，靠 Config 驱动。

❌ **但有 3 处实际需要条件分支**（即使抽象得再好）：

| 位置 | 现有行为 | Desktop 模式要求 |
|------|---------|-----------------|
| `services/sandbox_manager.py:115` | `self._bwrap_available = shutil.which("bwrap")` | Windows 上 bwrap 不存在 → `subprocess.run` 回退（无隔离） |
| `main.py:192-226` | `FUSE_ENABLED=True` 启动 pyfuse3 守护进程 | Windows 没有 `/dev/fuse` → 必须设置 `FUSE_ENABLED=false` |
| `services/tools/bash_tools.py:34-66` | `python3`, `cp`, `mv`, `rm` 等白名单 + Linux 路径 | Windows 上没有 `/bin/bash`，`rm` ≠ `del` |

**结论**: 引擎抽象层不感知"Desktop"或"Server"是可行的，但**底层子系统（沙箱/FUSE/bash）必须按平台退化**，这需要：
- 把 `STORAGE_MODE` / `FUSE_ENABLED` / `SANDBOX_MAX_CONCURRENT` 等**配置化**（已部分实现，但默认值仍偏 Linux）
- 而不是加 `if MODE == "desktop"` 分支

文档第 3.3 节"不需要改的文件"过于乐观 — `bash_tools.py` 在 Windows 上即便能跑，行为也错乱。

### 1.2 `config.py` 改动 — 🔴 **P0 含事实错误**

**文档原话**:
```python
STORAGE_MODE: str = "cos"         # 默认 cos，无配置时须显式改为 local
LOCAL_STORAGE_ROOT: str = ""      # 空 = ~/.feclaw/data/
```

**代码验证（`config.py:97-99`）**:
```python
STORAGE_MODE: str = "auto"                    # ← 实际是 "auto"，不是 "cos"
LOCAL_STORAGE_ROOT: str = "./feclaw-storage"  # ← 是相对路径，不是空
```

🔴 **P0 错误**:
1. 文档把 `STORAGE_MODE` 默认值写成 `"cos"`，实际是 `"auto"`。`"auto"` 模式有 COS 配置走 COS，无则降级到本地（`services/file_storage.py:106-110`），这个**比文档描述的方案更友好**，文档应顺势采用 `"auto"` 作为默认。
2. `LOCAL_STORAGE_ROOT: str = ""` 不存在 — 实际是相对路径 `"./feclaw-storage"`。这意味着：Desktop 用户从 CWD 启动时会写到任意位置，**不可移植**。
3. `BIND_HOST: str = "0.0.0.0"` 已经存在（`config.py:15` 的 `HOST`），文档重新命名为 `BIND_HOST` 是没有必要的重复。

🟡 **P1 建议**:
- `AUTH_DISABLE_LOCAL: bool = False`（文档 3.1 节）— 这个标志的语义和现有 `OAUTH_ENABLED` 重复。**建议统一**：
  - 现有 `OAUTH_ENABLED` 是基于 OAuth Provider 是否启用的语义
  - 文档的 `AUTH_DISABLE_LOCAL` 是基于网络位置的语义
  - 二者可共存，但**默认值策略需明确**：Desktop 启动时如果 `BIND_HOST=127.0.0.1` 是否自动 `AUTH_DISABLE_LOCAL=True`？

### 1.3 `~/.feclaw/` 目录规范 — 🟡 **P1**

**文档第 3.2 节**:
```
~/.feclaw/
├── data/              ← LOCAL_STORAGE_ROOT
├── feclaw.db          ← SQLite 数据库
├── vectors/
├── config.toml
├── logs/
└── .first_run
```

**代码验证（实际散落位置）**:
| 项 | 现状 | 路径 |
|----|------|------|
| 数据库 | `config.py:36` | `data/feclaw.db`（相对 BASE_DIR）|
| 文件存储 | `config.py:99` | `./feclaw-storage`（相对 CWD）|
| 向量存储 | `config.py:103` | `data/vectors.db`（相对 CWD）|
| 沙箱临时 | `sandbox_manager.py:78` | `/tmp/sandbox/{user_id}`（硬编码 Linux）|
| VFS 缓存 | `sandbox_manager.py:82` | `/tmp/vfs-cache/{user_id}/`（硬编码 Linux）|
| FUSE 挂载 | `config.py:185` | `/tmp/feclaw-fuse`（硬编码 Linux）|
| bwrap BPF | `sandbox_manager.py:603` | `/tmp/seccomp_bpf.bin`（硬编码 Linux）|

🔴 **P0**: **沙箱临时目录、VFS 缓存目录、FUSE 挂载点全是硬编码 `/tmp/...`**，这些在 Windows 上完全不能用。即使忽略 FUSE，**沙箱代码也无法在 Windows 上运行**（包括 Desktop 模式）。

🟡 **P1**: 文档列出的 6 个子目录中有 2 个（`vectors/`、`logs/`）**没有现有代码对应**：
- `data/vectors.db` 实际是 SQLite 文件（不是目录）
- `logs/` 完全没有目录约定
- 文档应说明这两项是新增的还是已存在的别名

🟡 **P1**: `.first_run` 哨兵文件 — **现有 main.py:133-149 已经在每次启动时检测 admin 用户是否存在并自动创建**，密码是 `sha256("admin")`。**不需要 `.first_run` 哨兵**。文档方案"首次启动自建 admin + 密码打印到终端"应改为：检测 admin 是否存在时**生成强随机密码并打印**（或者保留默认 `admin` 但强制 Web 端首次登录改密）。

---

## 2. 桌面与服务器差异点验证

### 2.1 第 2.1 节"必须不同的点"逐项审计

#### ✅ `文件存储 CosStorage ↔ LocalStorage` — 已实现

`services/file_storage.py:77-112` 抽象层已就绪，`local_storage.py:1-103` 完整实现。
✅ **验证通过**。

#### ✅ `向量搜索 CosVectorStorage ↔ SqliteVecStorage / NumpyVecStorage` — 已实现

文档标注"已实现"，代码确认存在 `vector_search_service.py`，未发现遗漏。
✅ **验证通过**。

#### 🔴 `代码执行 bwrap → subprocess + Web 弹窗` — P0

**文档原话**: "🔄 待设计"

**代码验证（`services/sandbox_manager.py`）**:
- 行 426-440：构建 bwrap 命令（含 netns、seccomp enforcer）
- 行 522-548：`_execute_with_bwrap` 通过 `subprocess.run(bwrap_cmd)` 执行
- 行 603-604：`SECCOMP_ENFORCER_PATH = "/usr/local/libexec/feclaw/seccomp-enforcer"`（硬编码 Linux）
- 行 604：`BWRAP_BPF_PATH = "/tmp/seccomp_bpf.bin"`（硬编码 Linux）

🔴 **P0**: Desktop 模式下 bwrap 二进制和 seccomp-enforcer 都**根本不存在**。即使系统回退到 `_execute_with_subprocess_safe`（行 561-600），也是 `python3 script_path` 在 `/tmp` 下执行，**Windows 上 `/tmp` 不存在**。

**结论**: Desktop 模式必须：
1. 禁用整个 `SandboxManager` 改用直接 `subprocess`（无隔离但能跑）
2. 或者使用 Windows 特定的隔离机制（`AppContainer` API）
3. 这需要在 `bash_tool.py` 顶层根据平台/模式选择沙箱后端

**文档的 ConsentManager 思路（5.1 节）**对 Desktop 是正确的方向，但**它解决的是"用户确认"问题，不是"代码隔离"问题**。这两件事必须分别处理：
- **隔离层**（Platform 强制）: bwrap/Windows AppContainer/无隔离降级
- **确认层**（Desktop 弹窗）: ConsentManager

#### 🔴 `用户认证 OAuth + TOTP → 首次启动自建 admin` — P0

**代码验证**:
- `main.py:133-149` **已自动创建 admin**（密码硬编码 SHA-256("admin")，**极不安全**）
- `routers/user.py:42, 148` 已实现 OAuth 禁用本地注册的逻辑
- `config.py:79-83` 已实现 `OAUTH_ENABLED` property
- `AUTH_DISABLE_LOCAL`（文档新增）**与 `OAUTH_ENABLED` 语义重叠**

🔴 **P0**: 文档说"首次启动自建 admin，密码打印到终端"，但现有代码已**在每次启动都创建密码为 `admin` 的用户**。这个用户已经存在于所有现有部署。Desktop 模式启动会复用这个用户，**用户以为拿到了随机密码，实际登录是 `admin/admin`**。

**强制要求**:
1. 文档应明确：**Desktop 模式必须强制用户首次登录后改密**，否则安全隐患巨大
2. 或者改成：首次启动时**生成强随机密码并打印到终端 + Web 端首次登录强制改密**

#### 🔴 `前端文件上传 预签名 URL 直传 COS → POST body 写本地` — P0

**代码验证**:
- `static/cos-js-sdk-v5.min.js` 存在（前端依赖 COS JS SDK）
- `routers/feclaw_domain.py:791`: `signed_url = s().generate_presigned_put_url(full_path, body.expires)`（生成 COS 预签名 URL）
- `routers/feclaw_domain.py:796`: `signed_url = s().generate_presigned_get_url(...)`（生成 COS 预签名下载 URL）
- `routers/workspace.py:392`: `file: UploadFile = File(...)` （已有后端直传端点）

🔴 **P0**: 前端**默认走 COS 直传**，Desktop 用户无 COS 凭证时这一路径会全部失败：
1. 前端初始化拿到 `signed_url` 是 COS URL → `fetch(put, signed_url)` 失败
2. 即使有 `UploadFile` 端点（`routers/workspace.py:392`），前端可能没接

**强制要求**:
- 文档应明确指明**前端需要修改**（或加配置开关）
- 建议在 `create_file_storage` 的基础上加 `get_presigned_put_url(key)` 抽象方法：COS 返回 COS URL，Local 返回后端 `/api/upload/direct` 端点
- 修改位置：`routers/feclaw_domain.py:780-800` 附近的 `signed_url` 生成逻辑

#### 🔴 `前端 CDN COS 静态网站托管 → feclaw serve-static 内置` — P0

**代码验证（`services/static_site_service.py`）**:
- 行 82：`COS_STATIC_SITES_PREFIX = "firstentrance/static-sites/"`
- 行 243-355：硬编码 `https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/...`
- 行 411：`get_cos_key` 硬编码 COS 路径
- 行 408：`{site.subdomain}.site.firstentrance.net`（固定子域名前缀）

🔴 **P0**: **整个 `StaticSiteService` 都深度依赖 COS**：
- `cos_prefix` 参数贯穿全文件
- 公开访问（`routers/static_site_public.py`）走 `storage_service.get_file_content(cos_key)`，本可移植，但 `static_site_service.py:355` 仍硬编码 COS URL

**Desktop 模式策略选项**:
- 选项 A: 禁用静态网站托管（`/api/static-site/*` 返回 503）
- 选项 B: 端口转发（`http://127.0.0.1:8080/sites/{subdomain}/` 通过 FastAPI 直接服务）
- 选项 C: 用 Python `http.server` 起子进程

文档的"`feclaw serve-static` 内置"过于笼统 — 实际工作量较大。

### 2.2 第 2.2 节"完全相同"逐项审计

✅ LLM/工具/ChatService/WeChat/TOTP/Memory/Cron — 全部跨平台，**验证通过**。

🟡 **P1**: 但 `TOTP` 跨平台不等于"开箱即用"：
- `services/totp_service.py` 需要 `pyotp` + `qrcode`
- 桌面用户首次启动时**是否自动注册 TOTP？** 文档未说
- 现有 main.py 没看到 TOTP 自动注册逻辑

---

## 3. 代码执行确认（第 5 章）

### 3.1 ConsentManager 架构 — ✅ 可行

**文档原话** (5.1):
```
bash_tool.py 检测 mode
  → Desktop: 不执行，走 ConsentManager
      → POST /api/sandbox/consent-request
          → 桌面客户端轮询 /api/sandbox/consent-pending
              → Tauri Rust 侧弹原生窗
                  → 用户点允许/拒绝/始终允许
              → POST /api/sandbox/consent-response
                  → bash_tool.py 继续/终止
```

**评估**:
- ✅ 协议设计合理（轮询而非 WebSocket 简化实现）
- ✅ 现有 `routers/sandbox.py:34` 已有 `APIRouter(prefix="/api/sandbox")`，可直接挂载
- ✅ bash_tool.py:30-66 的执行入口清晰，可以插入 pre-execute 钩子

🟡 **P1 改进**:
1. **轮询频率未定义** — 建议 500ms-1s，附 timeout
2. **consent-request 应包含** `agent_hash`、`sandbox_id`、`original_command`、`risk_level`、`working_dir`、**完整命令历史**
3. **始终允许**应支持**会话级 + 永久级**两种粒度
4. **超时行为** — 用户 5 分钟不响应应如何处理？默认拒绝？自动取消？

### 3.2 风险等级分类逻辑 — 🟡 **P1**

**文档第 5.2 节**:
| 等级 | 操作 | 行为 |
|------|------|------|
| L1 | 读文件 (cat, ls, grep) | 静默执行 |
| L2 | 写文件 (echo >, cp, mv) | Web 弹窗 |
| L3 | 删文件 (rm, del) | 原生系统级弹窗 + 红色 |
| L4 | 网络 (curl, wget) | Web 弹窗 |
| L5 | Python 代码执行 | Web 弹窗 |

**代码验证**:
- `services/tools/bash_tools.py:19`: `ALLOWED_BASH_COMMANDS = {"mkdir", "ls", "cat", "grep", "find", "head", "tail", "wc", "echo", "pwd", "cd", "cp", "mv", "rm"}` — **已有命令分类基础**，但 L1/L2 区分没有（`cat` 和 `echo >` 在同一白名单里）
- `services/tools/bash_tools.py:21`: `_SHELL_METACHARS` 正则拒绝 `><|;&`$\`` — 已经对**重定向/管道/子 shell** 做了元字符拦截

🟡 **P1 问题**:
1. **L2 难以精确分类** — `echo "hello"` 是无害写 stdout；`echo "x" > file.py` 是写文件；`echo "x" | nc` 是网络。靠命令名分类不准确
2. **建议**:
   - **模式匹配**而非命令分类：检测 `>`, `>>`, `tee`, `cp`, `mv` 触发 L2
   - `rm` 直接是 L3（已精确）
   - 检测 `curl`/`wget`/`nc`/`http` 触发 L4
   - `python3` 触发 L5
3. **风险等级可放 consent_request body 一起传给前端**，前端按等级选弹窗风格（红/黄/蓝）

### 3.3 轮询 `/api/sandbox/consent-pending` — ✅ 合理

✅ **验证通过**。但**Q 待定**：
- 轮询粒度：1s 还是 500ms？
- 跨进程：Desktop 重启后，pending consent 是否还活着（需要持久化？）

### 3.4 WeChat 降级确认（5.3 节）— 🟡 **P1 + Q**

**文档原话**:
> "如果 Desktop 关闭了浏览器窗口，但 WeChat 发来了消息：Agent 用 WeChat 通道回复：'🛡️ 我需要执行：rm file.txt，回复 Y 确认 / N 拒绝'"

🟡 **P1 评估**:
- 想法巧妙但**实现复杂**：需要 Agent 在执行前**主动**生成"确认请求"消息、追踪 WeChat 用户回复、把 Y/N 映射到 consent-response
- 现有 `chat_service.py` 是按 token 流式输出，不会中途"挂起等用户回复"
- 实际工作量至少 2-3 天

🟡 **Q 待定**:
- 5 分钟无回复：取消？等？
- WeChat 已被用于接收消息，**额外确认消息是否会让用户混淆**？

---

## 4. FeClaw-Desktop 客户端（第 4 章）

### 4.1 Tauri 选型 — 🟡 **P1**

**文档原话**:
> "推荐 Tauri 2.0：单个 exe <10MB，原生 WebView 渲染，Rust 后端能做系统托盘、原生弹窗、开机自启、进程管理。"

**评估**:

✅ **Tauri 优势**:
- 体积小（vs Electron）
- 系统托盘/原生弹窗/自启**确实是一等公民**（tray-icon / rfd / auto-launch crates）
- WebView 复用系统组件（macOS WKWebView / Windows WebView2 / Linux WebKitGTK）

🟡 **P1 反对 pywebview 的理由不充分**:
| 文档说法 | 反驳 |
|---------|------|
| "pywebview 没有系统托盘标准支持" | `pystray` 库可解决，纯 Python 跨平台 |
| "打包体积大（PyInstaller 打包 Python 解释器）" | PyInstaller 体积确实大，但**用户用 Desktop 一次，不在乎 200MB** |
| "进程管理麻烦（spawn 引擎进程后清理是噩梦）" | `psutil` + Windows `CREATE_NEW_PROCESS_GROUP` 可解 |
| "弹窗确认：只能用 ctypes.windll.user32.MessageBoxW" | 还有 `plyer` / `pywin32` / `tkinter` 等选择 |

🟡 **P1 真正的 Tauri 缺点**:
- **Rust 学习曲线** — 项目当前没 Rust 经验（CLAUDE.md 没提 Rust）
- **跨平台编译** — Tauri 在 Windows 上构建 Windows 二进制最自然；要 macOS 版本需 macOS
- **前端仍在 WebView** — 实际体验未必比 Web 浏览器好多少

**Q 待定**:
- 团队是否有 Rust 经验？
- 目标平台优先级（Windows / macOS / Linux）？
- 如果只做 Windows 平台，**pywebview + pystray 真的够用**（<2 周可出原型）

### 4.2 进程管理注意点 — 🔴 **P0**

**文档第 4.3 节伪代码**:
```rust
let engine = Command::new("feclaw")
    .args(["--port", "8080", "--host", "127.0.0.1"])
    .spawn();
```

🔴 **P0 隐患**:
1. **Windows 上 `feclaw` 不是可执行文件** — 需要 `.exe` 后缀（`feclaw.exe`）
2. **PATH 查找** — `Command::new` 不搜索 PATH，需要 `Command::new("feclaw.exe")` 配合 `which` crate 或硬编码绝对路径
3. **stdout/stderr 转发** — Rust 侧必须 pipe 子进程输出到日志窗口，否则用户看不到引擎崩溃信息
4. **优雅退出** — Windows 下 `Child::kill()` 是 `TerminateProcess`（硬杀），应该先 SIGTERM（Windows 是 CTRL_BREAK_EVENT）再 SIGKILL
5. **端口冲突** — 8080 已被占用时如何处理？应该自动换端口（如 8080-8099 范围扫描）
6. **健康检查** — 文档写"轮询 `/health`"，但 main.py:301-305 的健康检查**永远是 `{"status": "healthy"}`**，不会反映真实状态。需要扩展为 `lifespan` 内检查 DB/存储/FUSE 状态

**Q 待定**:
- 引擎崩溃后是否自动重启？最多几次？指数退避？
- 子进程用户身份：Desktop 进程用普通用户还是管理员？沙箱代码以什么权限运行？

### 4.3 不 pywebview 的原由（4.4 节）— 🟡 **P1 偏见**

见 4.1 节反驳。**P1 建议**: 至少做 pywebview 的 1 天 spike，验证假设。

---

## 5. 不对的地方（隐含改动 / 遗漏）

### 5.1 "引擎不需要改"——假的 🔴 **P0**

**实际需要改的文件清单**（按工作量）:

| 文件 | 改动 | 工作量 |
|------|------|--------|
| `config.py` | 改 `STORAGE_MODE` 默认值、`LOCAL_STORAGE_ROOT` 用 `~/.feclaw/...`、加 `AUTH_DISABLE_LOCAL`（或复用 `OAUTH_ENABLED`） | 0.5 天 |
| `main.py:78-80` | JWT_SECRET 必需 → 首次启动自动生成并持久化到 `~/.feclaw/.jwt_secret` | 0.5 天 |
| `main.py:133-167` | admin 用户密码**改为强随机** + 打印到终端 + 强制改密 | 0.5 天 |
| `services/sandbox_manager.py` | 检测 `sys.platform == "win32"`，Windows 路径全改 `%TEMP%` | 2-3 天 |
| `services/tools/bash_tools.py:34-66` | 白名单要兼容 Windows（`del`/`copy`/`type`），或强制禁用 bash 走 API 工具 | 1-2 天 |
| `routers/feclaw_domain.py:780-800` | presigned URL 生成改走 FileStorage 抽象（Local 返回 `/api/upload/direct`） | 1 天 |
| `static_site_service.py` | COS 硬编码全部解耦，Desktop 模式改走 `feclaw serve-static` 或内置路由 | 3-5 天 |
| `routers/oauth.py:36` | `_is_safe_redirect` 允许 `localhost`/`127.0.0.1`（已允许，但 main OAuth 流程在 Desktop 上完全不可用） | 0.5 天 |
| `routers/static_site_public.py` | 同样要适配 LocalStorage | 1 天 |
| `services/sandbox_manager.py:603-604` | `BWRAP_BPF_PATH` / `SECCOMP_ENFORCER_PATH` Windows 兼容 | 0.5 天 |

**总工作量**: **10-15 天**（不是文档说的 1-2 天）

### 5.2 文档遗漏的关键模块

❌ **未提及但需改造**:
1. **沙箱**（`services/sandbox/` 整个子目录）— 多个文件（`base.py` / `concurrency.py`）硬编码 `seccomp` / `bwrap` 假设
2. **静态网站托管**（`routers/static_site_public.py`）— 公开访问路径强依赖 COS
3. **Agent 5178 默认初始化**（`main.py:169-177`）— 首次启动会强制创建 Agent 5178，Desktop 模式下可能不必要
4. **OAuth 整套**（`routers/oauth.py` + `services/oauth_service.py`）— Desktop 上完全用不上
5. **邮件 / Webhook 通知**（如有）— Desktop 应改为系统通知
6. **日志**（`config.py:154-155`）— 文档说 `logs/`，但代码用 `logging.basicConfig` 输出到 stderr，没有文件 handler
7. **TLS / HTTPS** — Desktop 跑 HTTP，但前端 `static/js/auth.js:30` 有 `secure=True` cookie 设置，HTTP 下 cookie 不会被发送 → 登录失败
8. **CORS** — `main.py:289-296` 允许 `*`，Desktop 127.0.0.1 OK，但应锁定 origin
9. **`scripts/` 下 8 个迁移脚本**（`scripts/migrate_gaokao_index.py` 等）— Desktop 用户用不到，但应避免误启动

### 5.3 Desktop 模式下现有哪些代码会崩溃

| 路径 | 错误 | 原因 |
|------|------|------|
| `sandbox_manager.py:78` | `f"/tmp/sandbox/{user_id}"` | Windows 没有 `/tmp` |
| `sandbox_manager.py:82` | `f"/tmp/vfs-cache/{user_id}/"` | 同上 |
| `sandbox_manager.py:115` | `shutil.which("bwrap")` | Windows 没有 bwrap（会降级到 subprocess） |
| `sandbox_manager.py:529` | `f"/tmp/sandbox_exec_{sandbox_id}.py"` | 同上 |
| `sandbox_manager.py:603` | `"/tmp/seccomp_bpf.bin"` | 同上 |
| `sandbox_manager.py:604` | `"/usr/local/libexec/feclaw/seccomp-enforcer"` | Windows 没有 `/usr/local` |
| `bash_tools.py:29` | `python3` | Windows 上是 `python.exe` |
| `routers/feclaw_domain.py:36` | `f".{settings.FECLAW_DOMAIN}"` | FECLAW_DOMAIN 空时 cookie domain 为空 |
| `routers/oauth.py:60` | `secure=True` cookie | HTTP 下不被发送 |
| `main.py:78` | `if not settings.JWT_SECRET: raise` | Desktop 用户没 .env 直接挂 |

🔴 **P0**: 上述 10+ 个崩溃点都需在 Phase 1 处理。文档 1-2 天的估算**完全不够**。

---

## 6. 实施难度估算

### 6.1 Phase 1 重新估算

**文档估算**: 1-2 天

**实际工作量**:
| 任务 | 工作量 |
|------|--------|
| config.py 默认值 + `~/.feclaw/` 目录规范 | 0.5 天 |
| main.py JWT_SECRET 自动生成 | 0.5 天 |
| admin 用户强随机密码 + 终端打印 | 0.5 天 |
| SandboxManager 路径 Windows 兼容 | 2-3 天 |
| bash_tools.py 平台分支 | 1-2 天 |
| FUSE_ENABLED 默认 false 路径 | 0.5 天 |
| 修复 OAuth cookie secure 问题 | 0.5 天 |
| 测试 | 1-2 天 |
| **小计** | **7-11 天** |

🔴 **P0**: 文档低估 **5-7 倍**。

### 6.2 Phase 2 重新估算

**文档估算**: 3-5 天（Tauri 原型）

🟡 **P1 评估**:
- 假设团队无 Rust 经验，**3-5 天仅够搭脚手架**
- 进程管理 + 端口冲突 + 优雅退出 → **+2-3 天**
- 首次启动引导 + 自动开浏览器 → **+1-2 天**
- 系统托盘 + 开机自启 → **+1 天**
- Windows MSI 打包 → **+2-3 天**（Tauri 文档不全）
- **小计**: **9-14 天**

### 6.3 Phase 3 重新估算

**文档估算**: 2-3 天

🟡 **P1 评估**:
- ConsentManager 服务 + API 端点: 1 天
- Tauri 端 `show_confirm_dialog`: 0.5 天
- 风险等级分类（模式匹配而非命令分类）: **2-3 天**（需大量测试边界 case）
- 持久化 + 跨进程: 0.5 天
- WeChat 降级确认: **2-3 天**
- **小计**: **6-8 天**

### 6.4 Phase 4 重新估算

🟡 持续合理，但**自动更新**（GitHub Releases API）实际需要数字签名 + 增量更新 → **+2 周**

### 6.5 总工作量大改

| Phase | 文档估算 | 实际估算 |
|-------|---------|---------|
| Phase 1 | 1-2 天 | **7-11 天** |
| Phase 2 | 3-5 天 | **9-14 天** |
| Phase 3 | 2-3 天 | **6-8 天** |
| Phase 4 | 持续 | 持续 + 2 周（更新）|
| **总计** | **6-10 天** | **22-33 天 + 2 周** |

🔴 **P0 结论**: 文档整体低估 **3-4 倍**。

---

## 7. 额外问题（Q 待定）

### Q1: Desktop 是否支持多用户？
- 文档说"单用户系统"
- 但 OAuth admin 注册流程允许任意邮箱注册
- 建议 Desktop 模式**首次启动强制改 admin 密码后禁用 /api/user/register**

### Q2: Desktop 数据迁移路径？
- 用户已经在 Server 上用了 FeClaw，把 `~/.feclaw/data/` 拷到本地能恢复吗？
- LocalStorage 路径格式是 `feclaw/user_{user_id}/...`，与 Server COS 前缀一致 ✅
- 但 SQLite 数据库**不兼容**（Server 可能用 MySQL，Desktop 用 SQLite）
- 建议加 `feclaw import` / `feclaw export` 子命令

### Q3: Desktop 升级策略？
- 引擎热升级 vs. 冷升级
- 文档 Phase 4 提了"自动更新"但没说**如何回滚**
- Tauri 升级器支持回滚，但需要数字签名

### Q4: 网络代理 / 防火墙？
- Desktop 用户可能在公司网后
- LLM API（DeepSeek/Qwen）需 HTTPS 出网
- 沙箱默认 `--share-net`，**用户能 curl 到公司内网吗？** 这是安全风险

### Q5: 离线模式？
- LLM 调用需联网，断网时 Desktop 完全不可用
- 是否有计划支持本地 LLM（Ollama / llama.cpp）？
- 文档完全没提

---

## 8. 最终建议

### 必须修复（P0）
1. **重写 Phase 1 估算** — 实际工作量 7-11 天，**不是 1-2 天**
2. **JWT_SECRET 自动生成** — Desktop 用户不会读文档配置 .env
3. **admin 密码改强随机 + 打印** — 当前 SHA256("admin") 是定时炸弹
4. **SandboxManager 路径全平台化** — `/tmp/...` 全部用 `tempfile.gettempdir()`
5. **Presigned URL 走 FileStorage 抽象** — LocalStorage 需要 `get_presigned_put_url(key)` 返回后端直传端点
6. **static_site_service 全部解耦 COS** — Desktop 模式 `feclaw serve-static` 实际是路由 + StaticFiles 重写

### 建议改进（P1）
1. 用 `"auto"` 替代 `"cos"` 作为 `STORAGE_MODE` 默认值（与现状一致）
2. `AUTH_DISABLE_LOCAL` 与 `OAUTH_ENABLED` 合并，去除冗余
3. 风险等级改用**模式匹配**而非命令分类
4. WeChat 降级确认延后到 Phase 4，不是 Phase 3
5. 重新评估 pywebview（至少 1 天 spike）
6. JWT_SECRET 自动生成后写入 `~/.feclaw/.jwt_secret`（chmod 600）
7. 健康检查扩展为 `lifespan` 内真实状态（DB ping / Storage 读写 / FUSE 状态）
8. CORS 在 Desktop 模式锁定为 `["http://127.0.0.1:8080"]`

### 待定问题（Q）
- 团队 Rust 经验？
- 目标平台优先级？
- 是否支持离线 / 本地 LLM？
- 数据迁移 / 备份策略？
- 升级回滚策略？

### 验证通过（✅）
- 整体架构思路（引擎/壳解耦）
- FileStorage 抽象层
- 向量存储抽象层
- ConsentManager 协议设计
- LLM/工具/ChatService 跨平台性
- WeChat/TOTP/Memory/Cron 模块跨平台性

---

## 附录：审阅方法

本次审阅交叉对比了以下源码：
- `config.py`、`main.py`（lifespan + 路由注册）
- `services/file_storage.py`、`services/local_storage.py`、`services/storage_service.py`
- `services/virtual_filesystem.py`（storage 属性）
- `services/sandbox_manager.py`、`services/tools/bash_tools.py`
- `routers/__init__.py` + 所有 router 文件的 `include_router` 调用
- `routers/feclaw_domain.py`（presigned URL、UploadFile）
- `routers/sandbox.py`（沙箱 HTTP API）
- `routers/oauth.py`（OAuth 流程、cookie 设置）
- `routers/user.py`（注册逻辑）
- `services/static_site_service.py`（静态网站托管）
- `utils/auth.py`（JWT、密码哈希）
- `static/cos-js-sdk-v5.min.js`（前端 COS 直传）
- `docs/file-storage-audit-report.md`（已有的存储审计）

**总计审阅**: 15 个核心文件 + 4 个文档。
**未审阅**: Agent 工具集（`services/tools/ai_tools.py` 等 36+ 文件）、前端 HTML/JS、测试套件（应有 ~84 个测试需在 Phase 1 后回归）。
