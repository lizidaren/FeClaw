"""
Agent 配置界面页面

GET /console/ - Agent 管理控制台主页
GET /console/agents/new - 创建新 Agent
GET /console/agents/{agent_id}/config - 配置 Agent
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Agent Config UI", "Dashboard"])


@router.get("/console/", response_class=HTMLResponse)
@router.get("/console", response_class=HTMLResponse)
async def console_page(request: Request):
    """Agent 管理控制台主页"""
    return await _render_console_page(request)


@router.get("/console/agents/new", response_class=HTMLResponse)
async def new_agent_page(request: Request):
    """创建新 Agent 页面"""
    return await _render_new_agent_page(request)


@router.get("/console/agents/{agent_id}/config", response_class=HTMLResponse)
async def agent_config_page(request: Request, agent_id: int):
    """Agent 配置页面"""
    return await _render_agent_config_page(request, agent_id)


async def _render_console_page(request: Request):
    """Agent 管理控制台主页"""
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FeClaw Console - Agent 管理</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --primary: #667eea;
            --primary-dark: #5a67d8;
            --secondary: #764ba2;
            --bg-dark: #0a0a1a;
            --bg-card: #1a1a2e;
            --bg-input: #12122a;
            --text-light: #e0e0e0;
            --text-dim: #888;
            --text-muted: #666;
            --border: rgba(102, 126, 234, 0.2);
            --gradient: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --success: #4ade80;
            --warning: #fbbf24;
            --error: #f87171;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg-dark);
            color: var(--text-light);
            min-height: 100vh;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }

        .header h1 {
            font-size: 28px;
            font-weight: 700;
        }

        .header-actions {
            display: flex;
            gap: 12px;
        }

        .btn {
            padding: 12px 24px;
            border-radius: 12px;
            border: none;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
        }

        .btn-primary {
            background: var(--gradient);
            color: white;
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
        }

        .btn-secondary {
            background: var(--bg-card);
            color: var(--text-light);
            border: 1px solid var(--border);
        }

        .btn-secondary:hover {
            background: var(--bg-input);
        }

        .agents-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 20px;
        }

        .agent-card {
            background: var(--bg-card);
            border-radius: 16px;
            padding: 24px;
            border: 1px solid var(--border);
            transition: all 0.3s;
        }

        .agent-card:hover {
            border-color: var(--primary);
            transform: translateY(-4px);
            box-shadow: 0 8px 24px rgba(102, 126, 234, 0.2);
        }

        .agent-header {
            display: flex;
            justify-content: space-between;
            align-items: start;
            margin-bottom: 16px;
        }

        .agent-name {
            font-size: 18px;
            font-weight: 600;
        }

        .agent-hash {
            font-size: 14px;
            color: var(--primary);
            margin-top: 4px;
        }

        .status-badge {
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 500;
        }

        .status-pending { background: rgba(251, 191, 36, 0.2); color: var(--warning); }
        .status-initialized { background: rgba(74, 222, 128, 0.2); color: var(--success); }
        .status-suspended { background: rgba(248, 113, 113, 0.2); color: var(--error); }

        .agent-meta {
            color: var(--text-dim);
            font-size: 13px;
            margin-bottom: 16px;
        }

        .agent-actions {
            display: flex;
            gap: 8px;
        }

        .btn-sm {
            padding: 8px 16px;
            font-size: 13px;
            border-radius: 8px;
        }

        .empty-state {
            text-align: center;
            padding: 60px 20px;
        }

        .empty-state h2 {
            font-size: 24px;
            margin-bottom: 12px;
        }

        .empty-state p {
            color: var(--text-dim);
            margin-bottom: 24px;
        }

        .loading {
            text-align: center;
            padding: 40px;
            color: var(--text-dim);
        }

        @media (max-width: 768px) {
            .container { padding: 16px; }
            .header h1 { font-size: 22px; }
            .agents-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 Agent 管理</h1>
            <div class="header-actions">
                <button class="btn btn-secondary" onclick="window.location.href='/chat'">💬 聊天</button>
                <button class="btn btn-primary" onclick="window.location.href='/console/agents/new'">➕ 创建 Agent</button>
            </div>
        </div>

        <div id="agentsContainer" class="loading">
            加载中...
        </div>
    </div>

    <script>
        let jwt = localStorage.getItem('feclaw_jwt');

        document.addEventListener('DOMContentLoaded', () => {
            if (!jwt) {
                window.location.href = '/login';
                return;
            }
            loadAgents();
        });

        async function loadAgents() {
            try {
                const response = await fetch('/api/console/agents', {
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) {
                    if (response.status === 401) {
                        localStorage.removeItem('feclaw_jwt');
                        window.location.href = '/login';
                    }
                    throw new Error('Failed to load agents');
                }

                const data = await response.json();
                renderAgents(data.agents);
            } catch (error) {
                document.getElementById('agentsContainer').innerHTML = `
                    <div class="empty-state">
                        <h2>加载失败</h2>
                        <p>${error.message}</p>
                        <button class="btn btn-primary" onclick="loadAgents()">重试</button>
                    </div>
                `;
            }
        }

        function renderAgents(agents) {
            const container = document.getElementById('agentsContainer');

            if (!agents || agents.length === 0) {
                container.innerHTML = `
                    <div class="empty-state">
                        <h2>暂无 Agent</h2>
                        <p>创建你的第一个智能体吧！</p>
                        <button class="btn btn-primary" onclick="window.location.href='/console/agents/new'">
                            创建 Agent
                        </button>
                    </div>
                `;
                return;
            }

            container.className = 'agents-grid';
            container.innerHTML = agents.map(agent => `
                <div class="agent-card">
                    <div class="agent-header">
                        <div>
                            <div class="agent-name">${agent.name || 'Unnamed Agent'}</div>
                            <div class="agent-hash">#${agent.hash}</div>
                        </div>
                        <span class="status-badge status-${agent.status}">${agent.status}</span>
                    </div>
                    <div class="agent-meta">
                        创建于 ${formatDate(agent.created_at)}
                        ${agent.initialized_at ? `· 初始化于 ${formatDate(agent.initialized_at)}` : ''}
                    </div>
                    <div class="agent-actions">
                        ${agent.status === 'pending' ? 
                            `<button class="btn btn-sm btn-primary" onclick="initAgent(${agent.id})">初始化</button>` :
                            `<button class="btn btn-sm btn-secondary" onclick="configAgent(${agent.id})">配置</button>`
                        }
                        <button class="btn btn-sm btn-secondary" onclick="issueToken(${agent.id})">获取 Token</button>
                        <button class="btn btn-sm btn-secondary" onclick="deleteAgent(${agent.id}, '${agent.hash}')">删除</button>
                    </div>
                </div>
            `).join('');
        }

        async function initAgent(agentId) {
            try {
                const response = await fetch(`/api/console/agents/${agentId}/initialize`, {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${jwt}`,
                        'Content-Type': 'application/json'
                    }
                });

                if (!response.ok) throw new Error('Initialization failed');

                alert('Agent 初始化成功！');
                loadAgents();
            } catch (error) {
                alert('初始化失败: ' + error.message);
            }
        }

        function configAgent(agentId) {
            window.location.href = `/console/agents/${agentId}/config`;
        }

        async function issueToken(agentId) {
            try {
                const response = await fetch(`/api/console/agents/${agentId}/token`, {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) throw new Error('Failed to issue token');

                const data = await response.json();
                alert('Token 已签发:\n' + data.token + '\n\n有效期: ' + data.expires_in + ' 秒');
            } catch (error) {
                alert('签发失败: ' + error.message);
            }
        }

        async function deleteAgent(agentId, agentHash) {
            if (!confirm(`确定删除 Agent #${agentHash}？此操作不可恢复！`)) return;

            try {
                const response = await fetch(`/api/console/agents/${agentId}`, {
                    method: 'DELETE',
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) throw new Error('Failed to delete agent');

                alert('Agent 已删除');
                loadAgents();
            } catch (error) {
                alert('删除失败: ' + error.message);
            }
        }

        function formatDate(dateStr) {
            if (!dateStr) return '';
            const date = new Date(dateStr);
            return date.toLocaleDateString('zh-CN') + ' ' + date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        }
    </script>
</body>
</html>"""
    return html


async def _render_new_agent_page(request: Request):
    """创建新 Agent 页面"""
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FeClaw Console - 创建 Agent</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --primary: #667eea;
            --primary-dark: #5a67d8;
            --secondary: #764ba2;
            --bg-dark: #0a0a1a;
            --bg-card: #1a1a2e;
            --bg-input: #12122a;
            --text-light: #e0e0e0;
            --text-dim: #888;
            --text-muted: #666;
            --border: rgba(102, 126, 234, 0.2);
            --gradient: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --success: #4ade80;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg-dark);
            color: var(--text-light);
            min-height: 100vh;
        }

        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
        }

        .header {
            margin-bottom: 30px;
        }

        .back-link {
            color: var(--text-dim);
            text-decoration: none;
            font-size: 14px;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 20px;
        }

        .back-link:hover { color: var(--primary); }

        h1 {
            font-size: 28px;
            font-weight: 700;
        }

        .steps {
            display: flex;
            gap: 20px;
            margin-bottom: 30px;
            padding: 20px 0;
            border-bottom: 1px solid var(--border);
        }

        .step {
            display: flex;
            align-items: center;
            gap: 10px;
            color: var(--text-dim);
        }

        .step.active { color: var(--primary); }
        .step.completed { color: var(--success); }

        .step-number {
            width: 28px;
            height: 28px;
            border-radius: 14px;
            background: var(--bg-card);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            font-weight: 600;
        }

        .step.active .step-number { background: var(--gradient); }
        .step.completed .step-number { background: var(--success); }

        .form-card {
            background: var(--bg-card);
            border-radius: 16px;
            padding: 30px;
            border: 1px solid var(--border);
        }

        .form-group {
            margin-bottom: 24px;
        }

        .form-label {
            display: block;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 8px;
        }

        .form-input {
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 14px 16px;
            color: var(--text-light);
            font-size: 15px;
            font-family: inherit;
        }

        .form-input:focus {
            outline: none;
            border-color: var(--primary);
        }

        .form-input::placeholder {
            color: var(--text-muted);
        }

        .template-select {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 12px;
            margin-top: 12px;
        }

        .template-option {
            background: var(--bg-input);
            border: 2px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            cursor: pointer;
            transition: all 0.2s;
        }

        .template-option:hover {
            border-color: var(--primary);
        }

        .template-option.selected {
            border-color: var(--primary);
            background: rgba(102, 126, 234, 0.1);
        }

        .template-name {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 6px;
        }

        .template-desc {
            font-size: 13px;
            color: var(--text-dim);
        }

        .btn-group {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
            margin-top: 24px;
        }

        .btn {
            padding: 12px 24px;
            border-radius: 12px;
            border: none;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
        }

        .btn-primary {
            background: var(--gradient);
            color: white;
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
        }

        .btn-secondary {
            background: var(--bg-input);
            color: var(--text-light);
            border: 1px solid var(--border);
        }

        .btn-secondary:hover { background: var(--bg-card); }

        .step-content { display: none; }
        .step-content.active { display: block; }

        .error-message {
            background: rgba(248, 113, 113, 0.1);
            border: 1px solid rgba(248, 113, 113, 0.3);
            color: #f87171;
            padding: 12px 16px;
            border-radius: 10px;
            margin-top: 16px;
        }

        .success-message {
            background: rgba(74, 222, 128, 0.1);
            border: 1px solid rgba(74, 222, 128, 0.3);
            color: #4ade80;
            padding: 12px 16px;
            border-radius: 10px;
            margin-top: 16px;
        }

        @media (max-width: 768px) {
            .container { padding: 16px; }
            .steps { flex-direction: column; gap: 12px; }
            .template-select { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/console" class="back-link">← 返回控制台</a>
            <h1>创建新 Agent</h1>
        </div>

        <div class="steps">
            <div class="step active" id="step1">
                <div class="step-number">1</div>
                <div>基本信息</div>
            </div>
            <div class="step" id="step2">
                <div class="step-number">2</div>
                <div>选择模板</div>
            </div>
            <div class="step" id="step3">
                <div class="step-number">3</div>
                <div>初始化</div>
            </div>
        </div>

        <!-- Step 1: 基本信息 -->
        <div class="step-content active" id="stepContent1">
            <div class="form-card">
                <div class="form-group">
                    <label class="form-label">Agent 名称</label>
                    <input type="text" class="form-input" id="agentName" placeholder="给你的 Agent 起个名字">
                </div>
                <div class="btn-group">
                    <button class="btn btn-primary" onclick="nextStep(2)">下一步</button>
                </div>
            </div>
        </div>

        <!-- Step 2: 选择模板 -->
        <div class="step-content" id="stepContent2">
            <div class="form-card">
                <div class="form-group">
                    <label class="form-label">选择 Persona 模板</label>
                    <div class="template-select" id="templateSelect">
                        加载中...
                    </div>
                </div>
                <div class="btn-group">
                    <button class="btn btn-secondary" onclick="prevStep(1)">上一步</button>
                    <button class="btn btn-primary" onclick="createAgent()">创建</button>
                </div>
            </div>
        </div>

        <!-- Step 3: 初始化 -->
        <div class="step-content" id="stepContent3">
            <div class="form-card">
                <div id="initResult">
                    正在创建和初始化 Agent...
                </div>
            </div>
        </div>
    </div>

    <script>
        let jwt = localStorage.getItem('feclaw_jwt');
        let templates = {};
        let selectedTemplate = 'default';
        let currentAgent = null;

        document.addEventListener('DOMContentLoaded', () => {
            if (!jwt) {
                window.location.href = '/login';
                return;
            }
            loadTemplates();
        });

        async function loadTemplates() {
            try {
                const response = await fetch('/api/console/templates', {
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) throw new Error('Failed to load templates');

                const data = await response.json();
                templates = {};
                data.templates.forEach(t => templates[t.id] = t);
                renderTemplates(data.templates);
            } catch (error) {
                document.getElementById('templateSelect').innerHTML = `
                    <div style="padding: 20px; color: var(--text-dim);">
                        加载模板失败: ${error.message}
                    </div>
                `;
            }
        }

        function renderTemplates(templateList) {
            const container = document.getElementById('templateSelect');
            container.innerHTML = templateList.map(t => `
                <div class="template-option ${t.id === selectedTemplate ? 'selected' : ''}"
                     data-template-id="${t.id}" onclick="selectTemplate('${t.id}')">
                    <div class="template-name">${t.name}</div>
                    <div class="template-desc">${t.description}</div>
                </div>
            `).join('');
        }

        function selectTemplate(templateId) {
            selectedTemplate = templateId;
            document.querySelectorAll('.template-option').forEach(el => {
                el.classList.toggle('selected', el.dataset.templateId === templateId);
            });
        }

        function nextStep(step) {
            document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
            document.querySelectorAll('.step-content').forEach(c => c.classList.remove('active'));
            document.getElementById(`step${step}`).classList.add('active');
            document.getElementById(`stepContent${step}`).classList.add('active');

            // 标记完成的步骤
            for (let i = 1; i < step; i++) {
                document.getElementById(`step${i}`).classList.add('completed');
            }
        }

        function prevStep(step) {
            document.querySelectorAll('.step').forEach(s => {
                s.classList.remove('active', 'completed');
            });
            document.querySelectorAll('.step-content').forEach(c => c.classList.remove('active'));
            
            document.getElementById(`step${step}`).classList.add('active');
            document.getElementById(`stepContent${step}`).classList.add('active');

            // 标记完成的步骤
            for (let i = 1; i < step; i++) {
                document.getElementById(`step${i}`).classList.add('completed');
            }
        }

        async function createAgent() {
            const name = document.getElementById('agentName').value.trim() || '新 Agent';
            const persona = templates[selectedTemplate]?.persona || '';
            const templateId = selectedTemplate;

            nextStep(3);

            try {
                // 1. 创建 Agent
                const createResponse = await fetch('/api/console/agents', {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${jwt}`,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ name })
                });

                if (!createResponse.ok) throw new Error('Failed to create agent');

                const createData = await createResponse.json();
                currentAgent = createData.agent;

                // 2. 初始化 Agent（携带 template_id 让后端从 DB 加载 persona）
                const initResponse = await fetch(`/api/console/agents/${currentAgent.id}/initialize`, {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${jwt}`,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ persona, template_id: templateId })
                });

                if (!initResponse.ok) throw new Error('Failed to initialize agent');

                const initData = await initResponse.json();

                document.getElementById('initResult').innerHTML = `
                    <div class="success-message">
                        ✅ Agent 创建成功！
                    </div>
                    <div style="margin-top: 20px;">
                        <p><strong>名称:</strong> ${currentAgent.name}</p>
                        <p><strong>Hash:</strong> #${currentAgent.hash}</p>
                        <p><strong>状态:</strong> ${initData.agent.status}</p>
                    </div>
                    <div class="btn-group" style="margin-top: 24px;">
                        <button class="btn btn-secondary" onclick="window.location.href='/console'">返回控制台</button>
                        <button class="btn btn-primary" onclick="window.location.href='/console/agents/${currentAgent.id}/config'">配置 Agent</button>
                    </div>
                `;

                document.getElementById('step3').classList.add('completed');

            } catch (error) {
                document.getElementById('initResult').innerHTML = `
                    <div class="error-message">
                        ❌ 创建失败: ${error.message}
                    </div>
                    <div class="btn-group" style="margin-top: 24px;">
                        <button class="btn btn-secondary" onclick="prevStep(2)">返回修改</button>
                        <button class="btn btn-primary" onclick="createAgent()">重试</button>
                    </div>
                `;
            }
        }
    </script>
</body>
</html>"""
    return html


async def _render_agent_config_page(request: Request, agent_id: int):
    """Agent 配置页面"""
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FeClaw Console - Agent 配置</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --primary: #667eea;
            --primary-dark: #5a67d8;
            --secondary: #764ba2;
            --bg-dark: #0a0a1a;
            --bg-card: #1a1a2e;
            --bg-input: #12122a;
            --bg-preview: #0d1117;
            --text-light: #e0e0e0;
            --text-dim: #888;
            --text-muted: #666;
            --border: rgba(102, 126, 234, 0.2);
            --gradient: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --success: #4ade80;
            --warning: #fbbf24;
            --error: #f87171;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg-dark);
            color: var(--text-light);
            min-height: 100vh;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }

        .header-left {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .back-link {
            color: var(--text-dim);
            text-decoration: none;
            font-size: 14px;
        }

        .back-link:hover { color: var(--primary); }

        .header h1 {
            font-size: 24px;
            font-weight: 700;
        }

        .agent-hash {
            color: var(--primary);
            font-size: 14px;
        }

        .tabs {
            display: flex;
            gap: 12px;
            margin-bottom: 24px;
        }

        .tab {
            padding: 12px 24px;
            background: var(--bg-card);
            border-radius: 12px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
            border: 1px solid transparent;
        }

        .tab:hover { background: var(--bg-input); }
        .tab.active {
            background: var(--gradient);
            color: white;
        }

        .config-section {
            display: none;
        }
        .config-section.active { display: block; }

        .two-column {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }

        .card {
            background: var(--bg-card);
            border-radius: 16px;
            padding: 24px;
            border: 1px solid var(--border);
        }

        .card-title {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 16px;
        }

        .form-group {
            margin-bottom: 20px;
        }

        .form-label {
            display: block;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 8px;
        }

        .form-input, .form-textarea, .form-select {
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 14px 16px;
            color: var(--text-light);
            font-size: 15px;
            font-family: inherit;
        }

        .form-textarea {
            min-height: 300px;
            resize: vertical;
            font-family: 'Fira Code', 'Consolas', monospace;
        }

        .form-input:focus, .form-textarea:focus, .form-select:focus {
            outline: none;
            border-color: var(--primary);
        }

        .preview-area {
            background: var(--bg-preview);
            border-radius: 12px;
            padding: 20px;
            min-height: 300px;
            overflow-y: auto;
        }

        .preview-area h1 { font-size: 1.4em; margin-bottom: 12px; }
        .preview-area h2 { font-size: 1.2em; margin-bottom: 10px; }
        .preview-area h3 { font-size: 1.1em; margin-bottom: 8px; }
        .preview-area p { margin-bottom: 8px; line-height: 1.6; }
        .preview-area ul, .preview-area ol { margin-left: 20px; margin-bottom: 12px; }
        .preview-area li { margin-bottom: 4px; }
        .preview-area code {
            background: rgba(102, 126, 234, 0.15);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'Fira Code', monospace;
        }
        .preview-area pre {
            background: #0d1117;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 12px;
        }
        .preview-area pre code { background: transparent; padding: 0; }

        .tools-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 12px;
        }

        .tool-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 12px;
            background: var(--bg-input);
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.2s;
        }

        .tool-item:hover { background: var(--bg-card); }
        .tool-item.enabled { border: 1px solid var(--success); }
        .tool-item.disabled { border: 1px solid var(--border); opacity: 0.6; }

        .tool-checkbox {
            width: 18px;
            height: 18px;
            accent-color: var(--primary);
        }

        .tool-name {
            font-size: 14px;
            font-weight: 500;
        }

        .tool-group-title {
            font-size: 13px;
            color: var(--text-dim);
            margin-bottom: 10px;
            padding-top: 10px;
        }

        .btn {
            padding: 12px 24px;
            border-radius: 12px;
            border: none;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
        }

        .btn-primary {
            background: var(--gradient);
            color: white;
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
        }

        .btn-secondary {
            background: var(--bg-input);
            color: var(--text-light);
            border: 1px solid var(--border);
        }

        .btn-secondary:hover { background: var(--bg-card); }

        .btn-group {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
            margin-top: 24px;
        }

        .status-message {
            padding: 12px 16px;
            border-radius: 10px;
            margin-top: 16px;
            display: none;
        }

        .status-success {
            display: block;
            background: rgba(74, 222, 128, 0.1);
            border: 1px solid rgba(74, 222, 128, 0.3);
            color: var(--success);
        }

        .status-error {
            display: block;
            background: rgba(248, 113, 113, 0.1);
            border: 1px solid rgba(248, 113, 113, 0.3);
            color: var(--error);
        }

        .style-options {
            display: flex;
            gap: 10px;
        }

        .style-option {
            padding: 10px 20px;
            background: var(--bg-input);
            border: 2px solid var(--border);
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.2s;
        }

        .style-option:hover { border-color: var(--primary); }
        .style-option.selected {
            border-color: var(--primary);
            background: rgba(102, 126, 234, 0.1);
        }

        .loading {
            text-align: center;
            padding: 40px;
            color: var(--text-dim);
        }

        @media (max-width: 768px) {
            .container { padding: 16px; }
            .two-column { grid-template-columns: 1fr; }
            .tabs { flex-wrap: wrap; }
            .tools-grid { grid-template-columns: 1fr 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-left">
                <a href="/console" class="back-link">← 控制台</a>
                <h1 id="agentName">加载中...</h1>
                <span class="agent-hash" id="agentHash"></span>
            </div>
        </div>

        <div class="tabs">
            <div class="tab active" onclick="switchTab('persona')">Persona</div>
            <div class="tab" onclick="switchTab('style')">Style</div>
            <div class="tab" onclick="switchTab('tools')">Tools</div>
        </div>

        <!-- Persona Section -->
        <div class="config-section active" id="personaSection">
            <div class="two-column">
                <div class="card">
                    <div class="card-title">编辑 Persona</div>
                    <div class="form-group">
                        <label class="form-label">Persona 内容 (Markdown)</label>
                        <textarea class="form-textarea" id="personaInput" 
                                  oninput="updatePreview()"
                                  placeholder="输入 Persona 内容..."></textarea>
                    </div>
                    <div class="form-group">
                        <label class="form-label">预设模板</label>
                        <select class="form-select" id="templateSelect" onchange="applyTemplate()">
                            <option value="">-- 选择模板 --</option>
                        </select>
                    </div>
                    <div class="btn-group">
                        <button class="btn btn-primary" onclick="savePersona()">保存</button>
                    </div>
                    <div class="status-message" id="personaStatus"></div>
                </div>

                <div class="card">
                    <div class="card-title">实时预览</div>
                    <div class="preview-area" id="personaPreview">
                        预览区域
                    </div>
                </div>
            </div>
        </div>

        <!-- Style Section -->
        <div class="config-section" id="styleSection">
            <div class="card">
                <div class="card-title">回复风格</div>
                <div class="form-group">
                    <label class="form-label">选择 Agent 的回复风格</label>
                    <div class="style-options" id="styleOptions">
                        <div class="style-option" onclick="selectStyle('professional')">
                            <strong>专业</strong>
                            <small style="color: var(--text-dim); display: block; margin-top: 4px;">严谨、正式</small>
                        </div>
                        <div class="style-option" onclick="selectStyle('friendly')">
                            <strong>友好</strong>
                            <small style="color: var(--text-dim); display: block; margin-top: 4px;">亲切、温和</small>
                        </div>
                        <div class="style-option" onclick="selectStyle('casual')">
                            <strong>随意</strong>
                            <small style="color: var(--text-dim); display: block; margin-top: 4px;">轻松、自然</small>
                        </div>
                        <div class="style-option" onclick="selectStyle('formal')">
                            <strong>正式</strong>
                            <small style="color: var(--text-dim); display: block; margin-top: 4px;">规范、礼貌</small>
                        </div>
                        <div class="style-option" onclick="selectStyle('creative')">
                            <strong>创意</strong>
                            <small style="color: var(--text-dim); display: block; margin-top: 4px;">灵活、新颖</small>
                        </div>
                    </div>
                </div>
                <div class="btn-group">
                    <button class="btn btn-primary" onclick="saveStyle()">保存</button>
                </div>
                <div class="status-message" id="styleStatus"></div>
            </div>
        </div>

        <!-- Tools Section -->
        <div class="config-section" id="toolsSection">
            <div class="card">
                <div class="card-title">工具配置</div>
                <div id="toolsConfig">
                    加载中...
                </div>
                <div class="btn-group">
                    <button class="btn btn-primary" onclick="saveTools()">保存</button>
                </div>
                <div class="status-message" id="toolsStatus"></div>
            </div>
        </div>
    </div>

    <script>
        let jwt = localStorage.getItem('feclaw_jwt');
        let agentId = null;
        let agentHash = '';
        let currentStyle = 'professional';
        let templates = {};
        let toolsConfig = { enabled: [], disabled: [] };

        // 工具分组
        const toolGroups = {
            '文件操作': ['file_read', 'file_write', 'file_list', 'file_delete'],
            '命令执行': ['bash', 'python_background', 'python_task_list', 'python_task_stop', 'python_task_output'],
            '网络工具': ['web_search'],
            '会话管理': ['end_conversation', 'list_conversations', 'load_conversation', 'generate_summary', 'search_sessions', 'auto_suggest_session'],
            '文件编辑': ['edit'],
            '定时任务': ['schedule_reminder', 'list_reminders', 'cancel_reminder'],
            '子Agent': ['spawn_subagent', 'list_subagent_roles'],
            '分享与安全': ['create_share_link', 'generate_totp']
        };

        document.addEventListener('DOMContentLoaded', () => {
            // 从 URL 获取 agent_id
            const pathParts = window.location.pathname.split('/');
            agentId = parseInt(pathParts[pathParts.length - 2]);

            if (!jwt) {
                window.location.href = '/login';
                return;
            }

            loadAgentInfo();
            loadTemplates();
        });

        async function loadAgentInfo() {
            try {
                const response = await fetch(`/api/console/agents/${agentId}`, {
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) throw new Error('Failed to load agent');

                const data = await response.json();
                document.getElementById('agentName').textContent = data.agent.name;
                document.getElementById('agentHash').textContent = '#' + data.agent.hash;
                agentHash = data.agent.hash;

                // 加载各配置
                loadPersona();
                loadStyle();
                loadTools();
            } catch (error) {
                alert('加载 Agent 信息失败: ' + error.message);
            }
        }

        async function loadTemplates() {
            try {
                const response = await fetch('/api/console/templates', {
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) return;

                const data = await response.json();
                templates = {};
                data.templates.forEach(t => templates[t.id] = t);

                const select = document.getElementById('templateSelect');
                data.templates.forEach(t => {
                    const option = document.createElement('option');
                    option.value = t.id;
                    option.textContent = t.name;
                    select.appendChild(option);
                });
            } catch (error) {
                console.error('Load templates error:', error);
            }
        }

        async function loadPersona() {
            try {
                const response = await fetch(`/api/console/agents/${agentId}/persona`, {
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) return;

                const data = await response.json();
                if (data.status === 'success') {
                    document.getElementById('personaInput').value = data.persona;
                    updatePreview();
                }
            } catch (error) {
                console.error('Load persona error:', error);
            }
        }

        async function loadStyle() {
            try {
                const response = await fetch(`/api/console/agents/${agentId}/style`, {
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) return;

                const data = await response.json();
                currentStyle = data.style;
                updateStyleUI();
            } catch (error) {
                console.error('Load style error:', error);
            }
        }

        async function loadTools() {
            try {
                const response = await fetch(`/api/console/agents/${agentId}/tools`, {
                    headers: { 'Authorization': `Bearer ${jwt}` }
                });

                if (!response.ok) return;

                const data = await response.json();
                if (data.status === 'success') {
                    toolsConfig = data.tools;
                    renderToolsConfig();
                }
            } catch (error) {
                console.error('Load tools error:', error);
            }
        }

        function switchTab(tabName) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.config-section').forEach(s => s.classList.remove('active'));

            document.querySelector(`.tab[onclick="switchTab('${tabName}')"]`).classList.add('active');
            document.getElementById(`${tabName}Section`).classList.add('active');
        }

        function updatePreview() {
            const content = document.getElementById('personaInput').value;
            document.getElementById('personaPreview').innerHTML = marked.parse(content);
        }

        function applyTemplate() {
            const templateId = document.getElementById('templateSelect').value;
            if (templateId && templates[templateId]) {
                document.getElementById('personaInput').value = templates[templateId].persona;
                updatePreview();
            }
        }

        function selectStyle(style) {
            currentStyle = style;
            updateStyleUI();
        }

        function updateStyleUI() {
            document.querySelectorAll('.style-option').forEach(el => {
                const onclickStr = el.getAttribute('onclick');
                el.classList.toggle('selected', onclickStr && onclickStr.includes(currentStyle));
            });
        }

        function renderToolsConfig() {
            const container = document.getElementById('toolsConfig');
            let html = '';

            for (const [groupName, tools] of Object.entries(toolGroups)) {
                html += `<div class="tool-group-title">${groupName}</div>`;
                html += '<div class="tools-grid">';

                for (const tool of tools) {
                    const isEnabled = toolsConfig.enabled.includes(tool);
                    html += `
                        <div class="tool-item ${isEnabled ? 'enabled' : 'disabled'}" onclick="toggleTool('${tool}')">
                            <input type="checkbox" class="tool-checkbox" 
                                   ${isEnabled ? 'checked' : ''} 
                                   onclick="event.stopPropagation(); toggleTool('${tool}')">
                            <span class="tool-name">${tool}</span>
                        </div>
                    `;
                }

                html += '</div>';
            }

            container.innerHTML = html;
        }

        function toggleTool(toolName) {
            if (toolsConfig.enabled.includes(toolName)) {
                toolsConfig.enabled = toolsConfig.enabled.filter(t => t !== toolName);
                if (!toolsConfig.disabled.includes(toolName)) {
                    toolsConfig.disabled.push(toolName);
                }
            } else {
                toolsConfig.enabled.push(toolName);
                toolsConfig.disabled = toolsConfig.disabled.filter(t => t !== toolName);
            }
            renderToolsConfig();
        }

        async function savePersona() {
            const content = document.getElementById('personaInput').value;
            const statusEl = document.getElementById('personaStatus');

            try {
                const response = await fetch(`/api/console/agents/${agentId}/config`, {
                    method: 'PUT',
                    headers: {
                        'Authorization': `Bearer ${jwt}`,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ persona: content })
                });

                if (!response.ok) throw new Error('Failed to save');

                statusEl.className = 'status-message status-success';
                statusEl.textContent = '✅ Persona 已保存';
                setTimeout(() => statusEl.className = 'status-message', 3000);
            } catch (error) {
                statusEl.className = 'status-message status-error';
                statusEl.textContent = '❌ 保存失败: ' + error.message;
            }
        }

        async function saveStyle() {
            const statusEl = document.getElementById('styleStatus');

            try {
                const response = await fetch(`/api/console/agents/${agentId}/config`, {
                    method: 'PUT',
                    headers: {
                        'Authorization': `Bearer ${jwt}`,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ style: currentStyle })
                });

                if (!response.ok) throw new Error('Failed to save');

                statusEl.className = 'status-message status-success';
                statusEl.textContent = '✅ Style 已保存';
                setTimeout(() => statusEl.className = 'status-message', 3000);
            } catch (error) {
                statusEl.className = 'status-message status-error';
                statusEl.textContent = '❌ 保存失败: ' + error.message;
            }
        }

        async function saveTools() {
            const statusEl = document.getElementById('toolsStatus');

            try {
                const response = await fetch(`/api/console/agents/${agentId}/config`, {
                    method: 'PUT',
                    headers: {
                        'Authorization': `Bearer ${jwt}`,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ tools: toolsConfig })
                });

                if (!response.ok) throw new Error('Failed to save');

                statusEl.className = 'status-message status-success';
                statusEl.textContent = '✅ Tools 配置已保存';
                setTimeout(() => statusEl.className = 'status-message', 3000);
            } catch (error) {
                statusEl.className = 'status-message status-error';
                statusEl.textContent = '❌ 保存失败: ' + error.message;
            }
        }
    </script>
</body>
</html>"""
    return html