"""
Dashboard 页面 — 群聊消息实时浏览
"""
import logging
from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from typing import Optional

from models.database import get_db
from models.group import Group, GroupMember, GroupMessage
from models.agent_profile import AgentProfile
from utils.auth import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Dashboard"])

# Agent name mapping
AGENT_NAMES = {
    "4ce1": "程一凡",
    "be8e": "李姝含",
    "18a2": "王子昂",
    "5cb4": "沈初夏",
    "25d1": "陈思远",
    "62e4": "林一",
    "3dd0": "张昊",
    "43f9": "苏晴",
}

AGENT_COLORS = {
    "4ce1": "#667eea",
    "be8e": "#4ade80",
    "18a2": "#fbbf24",
    "5cb4": "#f87171",
    "25d1": "#a78bfa",
    "62e4": "#34d399",
    "3dd0": "#f59e0b",
    "43f9": "#f472b6",
}

AGENT_EMOJIS = {
    "4ce1": "🖥️",
    "be8e": "📋",
    "18a2": "🔧",
    "5cb4": "🎨",
    "25d1": "📊",
    "62e4": "🎭",
    "3dd0": "⚙️",
    "43f9": "📈",
}


@router.get("/dashboard/group/", response_class=HTMLResponse)
@router.get("/dashboard/group", response_class=HTMLResponse)
async def group_dashboard(request: Request):
    """群聊消息 Dashboard 页面"""
    html = PAGE_HTML
    return HTMLResponse(content=html)


@router.get("/dashboard/group/api/messages")
async def get_group_messages(
    request: Request,
    group_id: str = Query(default="4865a47a-1024-4ea6-aa82-5b789e81748c"),
    since: Optional[str] = Query(default=None, description="UTC timestamp ISO format"),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """API: 获取群聊消息（支持 since 参数实现增量轮询）"""
    uid = None
    try:
        uid = get_current_user_id(request)
    except Exception:
        pass

    if not uid:
        raise HTTPException(status_code=401, detail="未登录，请先登录")

    try:
        from datetime import datetime

        msgs_query = db.query(GroupMessage).filter(GroupMessage.group_id == group_id)

        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                msgs_query = msgs_query.filter(GroupMessage.created_at > since_dt)
            except Exception:
                pass

        msgs = msgs_query.order_by(GroupMessage.created_at.asc()).limit(limit).all()

        msg_list = []
        for m in msgs:
            sender_hash = m.sender_hash or ""
            name = AGENT_NAMES.get(sender_hash, "用户")
            emoji = AGENT_EMOJIS.get(sender_hash, "👤")

            msg_list.append({
                "id": m.id,
                "round": m.round,
                "sender_type": m.sender_type,
                "sender_hash": sender_hash,
                "sender_name": name,
                "emoji": emoji,
                "content": m.content,
                "mentions": m.mentions or [],
                "created_at": m.created_at.isoformat() if m.created_at else "",
            })

        total = db.query(GroupMessage).filter(GroupMessage.group_id == group_id).count()

        return JSONResponse(content={
            "messages": msg_list,
            "total": total,
            "count": len(msg_list),
        })
    finally:
        pass


PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FeClaw Group Chat Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --bg: #0a0a1a;
            --bg-card: #1a1a2e;
            --bg-msg: #12122a;
            --border: rgba(102, 126, 234, 0.15);
            --text: #e0e0e0;
            --text-dim: #888;
            --text-muted: #666;
            --primary: #667eea;
            --success: #4ade80;
            --warning: #fbbf24;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 0;
        }

        .header {
            background: var(--bg-card);
            border-bottom: 1px solid var(--border);
            padding: 16px 24px;
            position: sticky;
            top: 0;
            z-index: 100;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .header h1 {
            font-size: 18px;
            font-weight: 600;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .header .stats {
            font-size: 13px;
            color: var(--text-dim);
            display: flex;
            gap: 16px;
            align-items: center;
        }

        .header .stats span {
            background: var(--bg-msg);
            padding: 4px 10px;
            border-radius: 6px;
            border: 1px solid var(--border);
        }

        .status-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
            animation: pulse 1.5s ease-in-out infinite;
        }

        .status-dot.live { background: var(--success); }
        .status-dot.paused { background: var(--warning); }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 16px 16px 100px;
        }

        .msg-list {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .msg {
            padding: 12px 16px;
            border-radius: 8px;
            transition: background 0.2s;
            border-left: 3px solid transparent;
        }

        .msg:hover {
            background: rgba(255,255,255,0.03);
        }

        .msg.user {
            border-left-color: var(--primary);
        }

        .msg-head {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 4px;
        }

        .msg-avatar {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            flex-shrink: 0;
        }

        .msg-name {
            font-size: 13px;
            font-weight: 600;
        }

        .msg-round {
            font-size: 11px;
            color: var(--text-muted);
            padding: 1px 6px;
            border-radius: 4px;
            background: rgba(255,255,255,0.05);
        }

        .msg-time {
            font-size: 11px;
            color: var(--text-muted);
            margin-left: auto;
        }

        .msg-content {
            font-size: 14px;
            line-height: 1.7;
            white-space: pre-wrap;
            word-break: break-word;
            padding-left: 36px;
        }

        .msg-content blockquote {
            border-left: 2px solid var(--border);
            padding-left: 12px;
            margin: 8px 0;
            color: var(--text-dim);
            font-size: 13px;
        }

        .msg-content strong {
            color: var(--primary);
        }

        .msg-content .mention {
            color: var(--warning);
            font-weight: 500;
        }

        .msg-nav {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: var(--bg-card);
            border-top: 1px solid var(--border);
            padding: 12px 24px;
            display: flex;
            justify-content: center;
            gap: 12px;
            align-items: center;
        }

        .msg-nav button {
            background: var(--primary);
            color: white;
            border: none;
            padding: 8px 20px;
            border-radius: 8px;
            font-size: 13px;
            cursor: pointer;
            font-weight: 500;
            transition: opacity 0.2s;
        }

        .msg-nav button:hover { opacity: 0.85; }
        .msg-nav button:disabled { opacity: 0.4; cursor: not-allowed; }

        .msg-nav .auto-scroll-label {
            font-size: 12px;
            color: var(--text-dim);
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .empty-state {
            text-align: center;
            padding: 80px 20px;
            color: var(--text-muted);
        }

        .empty-state h2 { font-size: 20px; margin-bottom: 8px; }
        .empty-state p { font-size: 14px; }

        @media (max-width: 600px) {
            .container { padding: 8px; }
            .msg { padding: 8px 12px; }
            .msg-content { padding-left: 0; font-size: 13px; }
            .header h1 { font-size: 15px; }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>💬 Group Chat · 海洋资源创新方案讨论</h1>
        <div class="stats">
            <span id="msgCount">0 条</span>
            <span id="statusBadge"><span class="status-dot live"></span>直播中</span>
        </div>
    </div>

    <div class="container">
        <div id="msgList" class="msg-list">
            <div class="empty-state">
                <h2>📭 加载中...</h2>
                <p>请稍候，正在获取群聊消息</p>
            </div>
        </div>
    </div>

    <div class="msg-nav">
        <button id="scrollBottomBtn" onclick="scrollToBottom()">⬇ 滚动到底部</button>
        <label class="auto-scroll-label">
            <input type="checkbox" id="autoScroll" checked>
            自动滚动
        </label>
        <span id="newMsgIndicator" style="display:none;color:var(--warning);font-size:12px;">📩 有新消息</span>
    </div>

    <script>
        const GROUP_ID = "4865a47a-1024-4ea6-aa82-5b789e81748c";
        let lastTimestamp = "";
        let knownIds = new Set();
        let isLoading = false;

        // Get JWT token from cookie
        function getJwt() {
            const match = document.cookie.match(/(?:^|;\\s*)jwt=([^;]+)/);
            return match ? decodeURIComponent(match[1]) : null;
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function formatTime(isoStr) {
            if (!isoStr) return "";
            const d = new Date(isoStr);
            const h = String(d.getHours()).padStart(2, '0');
            const m = String(d.getMinutes()).padStart(2, '0');
            const s = String(d.getSeconds()).padStart(2, '0');
            return `${h}:${m}:${s}`;
        }

        function formatDate(isoStr) {
            if (!isoStr) return "";
            const d = new Date(isoStr);
            const month = d.getMonth() + 1;
            const day = d.getDate();
            return `${month}/${day}`;
        }

        function processContent(text) {
            // Escape HTML
            let html = escapeHtml(text);
            // @mentions (word boundary)
            html = html.replace(/@(\\S+)/g, '<span class="mention">@$1</span>');
            // Line breaks
            html = html.replace(/\\n/g, '<br>');
            // Bold (markdown-style)
            html = html.replace(/\\*\\*(\\S[^*]+\\S)\\*\\*/g, '<strong>$1</strong>');
            return html;
        }

        function renderMessage(m) {
            const isUser = m.sender_type === 'user';
            const time = formatTime(m.created_at);
            const date = formatDate(m.created_at);
            const round = m.round;

            return `
                <div class="msg ${isUser ? 'user' : ''}" data-msg-id="${escapeHtml(m.id)}">
                    <div class="msg-head">
                        <div class="msg-avatar">${m.emoji || '👤'}</div>
                        <span class="msg-name">${escapeHtml(m.sender_name)}</span>
                        <span class="msg-round">R${round}</span>
                        <span class="msg-time">${date} ${time}</span>
                    </div>
                    <div class="msg-content">${processContent(m.content)}</div>
                </div>
            `;
        }

        async function fetchMessages() {
            if (isLoading) return;
            isLoading = true;

            const jwt = getJwt();
            if (!jwt) {
                document.getElementById('msgList').innerHTML = \`
                    <div class="empty-state">
                        <h2>🔑 未登录</h2>
                        <p>请先登录后查看群聊消息</p>
                    </div>
                \`;
                document.getElementById('statusBadge').innerHTML = '<span class="status-dot paused"></span>未登录';
                isLoading = false;
                return;
            }

            try {
                let url = \`/dashboard/group/api/messages?group_id=\${GROUP_ID}&limit=500\`;
                if (lastTimestamp) {
                    url += \`&since=\${encodeURIComponent(lastTimestamp)}\`;
                }

                const resp = await fetch(url, {
                    headers: { 'Authorization': \`Bearer \${jwt}\` }
                });

                if (!resp.ok) {
                    if (resp.status === 401) {
                        document.getElementById('statusBadge').innerHTML = '<span class="status-dot paused"></span>认证过期';
                    }
                    isLoading = false;
                    return;
                }

                const data = await resp.json();
                const newMsgs = data.messages || [];

                // Update total count
                document.getElementById('msgCount').textContent = \`\${data.total} 条\`;
                document.getElementById('statusBadge').innerHTML = \`<span class="status-dot live"></span>直播中\`;

                if (newMsgs.length === 0) {
                    isLoading = false;
                    return;
                }

                // Update last timestamp
                const lastMsg = newMsgs[newMsgs.length - 1];
                lastTimestamp = lastMsg.created_at;

                // Filter out already known messages
                const actuallyNewMsgs = newMsgs.filter(m => !knownIds.has(m.id));

                if (actuallyNewMsgs.length === 0) {
                    isLoading = false;
                    return;
                }

                // Add to known set
                actuallyNewMsgs.forEach(m => knownIds.add(m.id));

                // Get message list container
                const container = document.getElementById('msgList');

                // Check if currently showing empty state
                const isEmpty = container.querySelector('.empty-state');
                if (isEmpty) {
                    container.innerHTML = '';
                }

                // Append new messages
                actuallyNewMsgs.forEach(m => {
                    const el = document.createElement('div');
                    el.innerHTML = renderMessage(m);
                    container.appendChild(el.firstElementChild || el);
                });

                // Auto-scroll if enabled
                const autoScroll = document.getElementById('autoScroll').checked;
                if (autoScroll) {
                    scrollToBottom();
                } else {
                    document.getElementById('newMsgIndicator').style.display = 'inline';
                }

            } catch (e) {
                console.error('Fetch error:', e);
            }

            isLoading = false;
        }

        function scrollToBottom() {
            window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
            document.getElementById('newMsgIndicator').style.display = 'none';
        }

        // Initial load + poll
        document.addEventListener('DOMContentLoaded', function() {
            fetchMessages();
            setInterval(fetchMessages, 3000);
        });
    </script>
</body>
</html>"""
