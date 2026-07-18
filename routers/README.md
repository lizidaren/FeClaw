# FeClaw 路由索引

| 文件 | 路径前缀 | 职责 |
|------|---------|------|
| `admin_panel.py` | `/admin` | 管理后台（配置/统计/部署） |
| `agent_config.py` | `/api/agent-config` | Agent 配置 CRUD |
| `agent_config_general.py` | `/api/agent-config/general` | Agent 通用设置 |
| `agent_llm.py` | `/api/agent-llm` | Agent LLM 配置 |
| `agent_tools.py` | `/api/agent-tools` | Agent 工具/技能开关 |
| `apps_gateway.py` | `/apps` | App 发布网关 |
| `chat.py` | `/api/chat` | 通用聊天 |
| `client_ws.py` | `/ws/client` | 客户端 WebSocket |
| `dashboard.py` | `/dashboard` | 用户控制台 |
| `desktop_api.py` | `/api/desktop` | 桌面端 API |
| `feclaw_chat.py` | `/api/chat` | Agent 聊天 |
| `feclaw_domain.py` | (工具) | 子域名解析 / Agent 路由 |
| `fehub.py` | `/api/fehub` | App 数据 & 发布 |
| `image.py` | `/api/image` | 图片生成 |
| `login.py` | `/login` | 登录页 |
| `metrics_internal.py` | `/api/metrics` | 系统指标 |
| `oauth.py` | `/api/oauth` | OAuth/OIDC 认证 |
| `setup.py` | `/setup` | 冷启动向导 |
| `share.py` | `/s` | 分享链接 |
| `static_site_public.py` | `/p` | 静态站点公开访问 |
| `user.py` | `/api/user` | 用户管理 |
| `zentrim.py` | `/api/zentrim` | Zentrim 笔记 |
