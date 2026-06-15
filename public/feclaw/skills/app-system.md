# FeClaw App 系统 — 创建与发布 Web 应用

Agent 可以注册路由端点，部署独立 Web 应用，让用户通过浏览器直接访问，无需经过对话。

## 前置条件

- Agent 已初始化（有 workspace 目录）
- 了解 VFS 文件操作：`file_read`, `file_write`, `file_list`

## 什么是 App

App 是一个包含多个页面的 Web 应用（如背单词网站、数学练习应用），由以下部分组成：

```
/workspace/apps/{app_id}/
  ├── routes.json       ← App 的路由定义（必选）
  ├── index.html        ← 页面（可选）
  ├── learn.html        ← 页面（可选）
  ├── style.css         ← 样式（可选）
  └── ...
```

路由定义示例（`routes.json`）：

```json
{
  "app_id": "vocab-app",
  "version": "1.0",
  "routes": [
    {"endpoint": "/",               "type": "static",  "file": "index.html",  "middleware": ["auth"]},
    {"endpoint": "/learn",          "type": "static",  "file": "learn.html"},
    {"endpoint": "/api/query",      "type": "ai",      "subagent": {"system_prompt": "词汇查询助手", "tools": ["web_search"], "tool_filter": {"allow": ["web_search"]}}},
    {"endpoint": "/api/word-list",  "type": "code",    "script": "list_words.py"}
  ],
  "middleware": {
    "auth": {"type": "platform_auth", "description": "要求登录 FirstEntrancePlatform"}
  }
}
```

三种响应类型：
- **static**: 直接返回文件内容（HTML/CSS/JS/图片），适合静态页面
- **ai**: 创建 SubAgent 处理请求，返回 JSON，适合需要 LLM 推理的动态内容
- **code**: 在 bwrap 沙箱中执行 Python 脚本，返回 stdout，适合结构化数据处理

## 创建并发布 App

### 第一步：从模板克隆

如果已有公有模板可用，直接复制到工作区：

```
cp /public/feclaw/templates/vocab-app /workspace/apps/my-vocab-app -r
```

查看可用模板：

```
file_list /public/feclaw/templates/
```

### 第二步：修改配置

编辑 `/workspace/apps/my-vocab-app/routes.json`，调整端点、权限等。

### 第三步：注册上线

```
route_register("my-vocab-app")
```

注册后用户可通过以下地址访问：

```
https://{agent_hash}.feclaw.lizidaren.cn/apps/my-vocab-app/
```

### 第四步：下线

```
route_unregister("my-vocab-app")
```

下线后 `/apps/my-vocab-app/` 返回 404，**不删除文件**，可随时重新注册。

## 示例：背单词应用

### 路由定义

```json
{
  "app_id": "vocab-app",
  "version": "1.0",
  "routes": [
    {"endpoint": "/", "type": "static", "file": "index.html"},
    {"endpoint": "/learn", "type": "static", "file": "learn.html"},
    {"endpoint": "/list", "type": "static", "file": "word-list.html"},
    {"endpoint": "/api/words", "type": "code", "script": "list_words.py"},
    {"endpoint": "/api/query", "type": "ai", "subagent": {
      "system_prompt": "你是词汇查询助手。返回 JSON 格式：{\"word\": string, \"definition\": string, \"examples\": [string]}",
      "tools": ["web_search"],
      "max_turns": 2
    }}
  ]
}
```

### API 示例

查询单词：
```bash
curl https://5656.feclaw.lizidaren.cn/apps/vocab-app/api/query \
  -H "Content-Type: application/json" \
  -d '{"word": "aberration"}'
```

返回：
```json
{
  "word": "aberration",
  "pronunciation": "/ˌæbəˈreɪʃn/",
  "definitions": [{"pos": "n.", "meaning": "偏离常规的行为", "examples": ["a mental aberration"]}],
  "synonyms": ["deviation", "anomaly"]
}
```

## 安全注意事项

- `type: "ai"` 路由：SubAgent 只能调用白名单工具（tool_filter），不能写文件
- `type: "code"` 路由：脚本在 bwrap 沙箱中执行，有内存/CPU/时间限制
- `type: "static"` 路由：路径校验防止 `../` 遍历攻击
- 可用 middleware 添加认证：`"middleware": ["auth"]` 要求 Platform 登录
- App 文件存放在 `/workspace/apps/{app_id}/`，不干扰 Agent 其他配置

## 限制

- 每个 Agent 可以注册最多 10 个 App
- code 脚本最大执行时间 30 秒
- 每个文件最大 10MB
