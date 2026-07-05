"""
Dashboard 页面 — 群聊消息实时浏览
"""
import logging
from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import Optional

from models.database import get_db
from models.group import Group, GroupMember, GroupMessage
from models.agent_profile import AgentProfile
from utils.auth import get_current_user_id, decode_jwt_token


async def get_current_user_id_optional_from_cookie(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
) -> Optional[int]:
    """从 Authorization header 或 cookie 获取 user_id"""
    # Try Authorization header first
    if credentials:
        token = credentials.credentials
        payload = decode_jwt_token(token)
        if payload and payload.get("user_id"):
            return payload["user_id"]

    # Try the feclaw_jwt cookie
    cookie_token = request.cookies.get("feclaw_jwt")
    if cookie_token:
        payload = decode_jwt_token(cookie_token)
        if payload and payload.get("user_id"):
            return payload["user_id"]

    return None

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
    group_id: str = Query(default="b20440ba-93f3-4390-864d-78912a607d3b"),
    since: Optional[str] = Query(default=None, description="UTC timestamp ISO format"),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user_id: Optional[int] = Depends(get_current_user_id_optional_from_cookie),
):
    """API: 获取群聊消息（支持 since 参数实现增量轮询）"""
    if not user_id:
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
            name = AGENT_NAMES.get(sender_hash)
            if not name:
                # 动态查询新 Agent
                try:
                    from models.database import AgentProfile
                    from models.database import SessionLocal as _SL
                    _d = _SL()
                    try:
                        _a = _d.query(AgentProfile).filter(AgentProfile.hash == sender_hash).first()
                        if _a:
                            name = _a.name
                    finally:
                        _d.close()
                except Exception:
                    pass
            if not name:
                name = "用户"
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
            gap: 12px;
        }

        .msg {
            padding: 16px 20px;
            border-radius: 10px;
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

        .msg-content code {
            background: rgba(102, 126, 234, 0.1);
            border: 1px solid var(--border);
            padding: 1px 5px;
            border-radius: 4px;
            font-size: 13px;
            color: var(--warning);
        }

        .msg-content pre {
            background: #0f0f23;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 16px;
            margin: 12px 0;
            overflow-x: auto;
        }

        .msg-content pre code {
            background: none;
            border: none;
            padding: 0;
            color: var(--text);
        }

        .msg-content table {
            border-collapse: collapse;
            width: 100%;
            margin: 12px 0;
            font-size: 13px;
        }

        .msg-content th, .msg-content td {
            border: 1px solid var(--border);
            padding: 8px 12px;
            text-align: left;
        }

        .msg-content th {
            background: rgba(102, 126, 234, 0.1);
            color: var(--primary);
            font-weight: 600;
        }

        .msg-content hr {
            border: none;
            border-top: 1px solid var(--border);
            margin: 16px 0;
        }

        .msg-content ul, .msg-content ol {
            padding-left: 24px;
            margin: 8px 0;
        }

        .msg-content li {
            margin: 4px 0;
        }

        .msg-content a {
            color: var(--primary);
            text-decoration: none;
        }

        .msg-content a:hover {
            text-decoration: underline;
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

    <script src="/static/marked.min.js"></script>
    <script>
        const GROUP_ID = "b20440ba-93f3-4390-864d-78912a607d3b";
        let lastTimestamp = "";
        let knownIds = new Set();
        let isLoading = false;

        // Get JWT token from cookie (feclaw_jwt) or localStorage
        function getJwt() {
            // Try localStorage first
            const lsToken = localStorage.getItem('feclaw_jwt');
            if (lsToken) return lsToken;
            // Fallback to cookie
            const match = document.cookie.match(/(?:^|;\\s*)feclaw_jwt=([^;]+)/);
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
            // Render markdown (uses marked.min.js from static)
            let html = marked.parse(text || '', { breaks: true, gfm: true });
            // @mentions after markdown so code blocks aren't affected
            html = html.replace(/@(\\S+)/g, '<span class="mention">@$1</span>');
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
                const ls = localStorage.getItem('feclaw_jwt');
                const ck = document.cookie.includes('feclaw_jwt');
                const debugInfo = 'localStorage: ' + (ls ? '有✅' : '无❌') + ' | cookie: ' + (ck ? '有✅' : '无❌');
                document.getElementById('msgList').innerHTML = `
                    <div class="empty-state">
                        <h2>🔑 未登录</h2>
                        <p>${debugInfo}</p>
                        <p style="margin-top:12px;font-size:12px;color:var(--warning)">请先登录。如果有登录信息但仍然显示未登录，请刷新页面重试。</p>
                    </div>
                `;
                document.getElementById('statusBadge').innerHTML = '<span class="status-dot paused"></span>未登录';
                isLoading = false;
                return;
            }

            try {
                let url = `/dashboard/group/api/messages?group_id=${GROUP_ID}&limit=500`;
                if (lastTimestamp) {
                    url += `&since=${encodeURIComponent(lastTimestamp)}`;
                }

                const resp = await fetch(url, {
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!resp.ok) {
                    const errText = await resp.text().catch(() => '(no body)');
                    document.getElementById('msgList').innerHTML = `
                        <div class="empty-state">
                            <h2>⚠️ ${resp.status === 401 ? '认证过期' : '请求失败'}</h2>
                            <p>HTTP ${resp.status}: ${escapeHtml(errText.slice(0,200))}</p>
                            <p style="margin-top:12px;font-size:12px">${resp.status === 401 ? '请重新登录' : '请检查网络后刷新'}</p>
                        </div>
                    `;
                    if (resp.status === 401) {
                        document.getElementById('statusBadge').innerHTML = '<span class="status-dot paused"></span>认证过期';
                    }
                    isLoading = false;
                    return;
                }

                const data = await resp.json();
                const newMsgs = data.messages || [];

                // Update total count
                document.getElementById('msgCount').textContent = `${data.total} 条`;
                document.getElementById('statusBadge').innerHTML = `<span class="status-dot live"></span>直播中`;

                if (newMsgs.length === 0) {
                    if (knownIds.size === 0) {
                        document.getElementById('msgList').innerHTML = `
                            <div class="empty-state">
                                <h2>📭 暂无消息</h2>
                                <p>该群聊还没有任何消息</p>
                            </div>
                        `;
                    }
                    isLoading = false;
                    return;
                }

                // Update last timestamp
                const lastMsg = newMsgs[newMsgs.length - 1];
                lastTimestamp = lastMsg.created_at;

                // Filter out already known messages
                const actuallyNew = newMsgs.filter(m => !knownIds.has(m.id));

                if (actuallyNew.length === 0) {
                    isLoading = false;
                    return;
                }

                // Add to known set
                actuallyNew.forEach(m => knownIds.add(m.id));

                // Get message list container
                const container = document.getElementById('msgList');

                // Check if currently showing empty state
                const isEmpty = container.querySelector('.empty-state');
                if (isEmpty) {
                    container.innerHTML = '';
                }

                // Append new messages
                actuallyNew.forEach(m => {
                    const div = document.createElement('div');
                    div.innerHTML = renderMessage(m);
                    container.appendChild(div.firstElementChild || div);
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
                document.getElementById('msgList').innerHTML = `
                    <div class="empty-state">
                        <h2>⚠️ 加载失败</h2>
                        <p>${escapeHtml(String(e.message || e))}</p>
                        <p style="margin-top:8px;font-size:12px">请检查网络后刷新页面</p>
                    </div>
                `;
                document.getElementById('statusBadge').innerHTML = '<span class="status-dot paused"></span>连接断开';
            }

            isLoading = false;
        }

        function scrollToBottom() {
            window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
            document.getElementById('newMsgIndicator').style.display = 'none';
        }

        // Initial load + poll
        document.addEventListener('DOMContentLoaded', function() {
            fetchMessages().then(() => {
                setInterval(fetchMessages, 3000);
            });
        });
    </script>
</body>
</html>"""
