# 会话管理工具说明

## 工具列表

### end_conversation
结束当前会话，并保存对话历史。

**触发语**（用户说以下类似的话时，请立即调用 `end_conversation()`，无需参数）：
- "开启新会话"、"new session"、"开始新会话"
- "结束这个会话"、"结束当前会话"
- "清除上下文"、"clear context"

### list_conversations
列出用户的所有会话列表。

**触发语**：
- 用户说"列出我的会话"

### load_conversation
加载指定的历史会话。

**参数**：
- `session_id`：会话ID（字符串）

**触发语**：
- 用户想加载某个历史会话

### search_sessions
搜索包含特定关键词的会话。

**参数**：
- `query`：搜索关键词（字符串）

**触发语**：
- 用户想搜索会话

## 注意
- 恢复会话时，用户会说 "resume:session_id"
- 会话ID格式类似 `session_xxx`
