# FeClaw Desktop WS 端实现日志

## Changes

### config.py
新增 DESKTOP_ENABLED 和 DESKTOP_WS_URL 配置字段

### routers/desktop_ws.py
新建文件 — Desktop WS 连接管理器 + /ws/desktop 端点
  - DesktopConnectionManager: connect/disconnect/send
  - /ws/desktop: WebSocket 路由
  - handle_desktop_message: 处理 consent_response
  - send_to_desktop(): 对外接口

### services/desktop_relay.py
新建文件 — Desktop 执行请求中继
  - DesktopRelay.request_consent(): 发送请求 + 等待响应
  - DesktopRelay.resolve_consent(): 收到响应后唤醒等待
  - relay.is_desktop_connected(): 检查连接状态

### main.py
注册 desktop_ws_router（条件：DESKTOP_ENABLED=true）

### services/tools/bash_tools.py
集成 desktop_relay.request_consent()
  - DESKTOP_ENABLED 时，若 bwrap 不可用则走 desktop_relay
  - 保留原有 bwrap 逻辑不变

### .env
追加 DESKTOP_ENABLED 和 DESKTOP_WS_URL
