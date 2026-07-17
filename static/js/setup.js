/* FeClaw 配置向导 — 前端交互逻辑
 *
 * 状态机：currentStep (1..6) + 各步骤的内存数据
 * 数据收集完成后，每个 step 在 next 时通过 fetch 写入 /setup/...
 *
 * 鉴权：
 * - 冷启动：URL ?token=<SETUP_TOKEN>（由 main.py 启动时打印到终端）
 * - 正常启动：localStorage.feclaw_jwt（与 utils/auth_dependencies 约定的 cookie 同源）
 *
 * 6 步：
 *   1) MySQL 配置 → POST /setup/database（仅测试连接）
 *   2) 管理员账号 → POST /setup/admin（建表 + 建 admin）
 *   3) 选择 API 平台
 *   4) 填入 API Key + 测试
 *   5) 选择默认模型
 *   6) 存储 / 向量后端 → POST /setup/storage + POST /setup/complete
 */

(function () {
    'use strict';

    // ──────────────────────────────────────────────
    // 鉴权模式：冷启动 token OR 正常启动 JWT
    // ──────────────────────────────────────────────
    const SETUP_TOKEN = (window.__FECLAW_SETUP_TOKEN__ || '').trim();
    const IS_COLD_START = !!SETUP_TOKEN;

    function getJwt() {
        try {
            return localStorage.getItem('feclaw_jwt') || '';
        } catch (e) {
            return '';
        }
    }

    function authHeaders() {
        if (IS_COLD_START) {
            return { 'X-Setup-Token': SETUP_TOKEN };
        }
        const t = getJwt();
        return t ? { 'Authorization': 'Bearer ' + t } : {};
    }

    function tokenQuery() {
        return IS_COLD_START ? ('?token=' + encodeURIComponent(SETUP_TOKEN)) : '';
    }

    const STATE = {
        currentStep: 1,
        // Step 1: DB
        db: {
            host: 'localhost',
            port: 3306,
            user: 'root',
            password: '',
            database: 'FeClaw',
            tested: false,   // 是否通过测试连接
        },
        // Step 2: Admin
        admin: { username: 'admin', password: '' },
        // Step 3-5: provider / key / model
        selectedProviders: new Set(),
        keys: {},
        verifyResults: {},
        providers: [],
        currentKeys: {},
        selectedModels: { text: '', vision: '', embedding: '', searchEngine: 'qwen' },
        // Step 6: storage
        storage: {
            storageMode: 'local',
            vectorStorageBackend: 'numpy',
            tencentCosSecretId: '',
            tencentCosSecretKey: '',
            tencentCosBucket: '',
        },
    };

    // ──────────────────────────────────────────────
    // 工具
    // ──────────────────────────────────────────────

    async function api(path, opts = {}) {
        const headers = Object.assign(
            { 'Content-Type': 'application/json' },
            opts.headers || {},
            authHeaders()
        );
        const resp = await fetch(path, Object.assign({}, opts, { headers }));
        if (resp.status === 401) {
            showToast('登录已过期，请重新登录', 'error');
            setTimeout(() => { window.location.href = '/login'; }, 1500);
            throw new Error('unauthorized');
        }
        if (resp.status === 403) {
            // Token 无效 → 冷启动：可能 token 被清掉（已重启）
            if (IS_COLD_START) {
                showToast('Setup token 已失效，请重启后端服务', 'error');
            } else {
                showToast('权限不足', 'error');
            }
            throw new Error('forbidden');
        }
        if (!resp.ok) {
            const text = await resp.text();
            throw new Error(`HTTP ${resp.status}: ${text}`);
        }
        return await resp.json();
    }

    function showToast(msg, kind = 'info') {
        const container = document.getElementById('toast-container');
        if (!container) return;
        const el = document.createElement('div');
        el.className = 'toast ' + kind;
        el.textContent = msg;
        container.appendChild(el);
        setTimeout(() => {
            el.style.opacity = '0';
            el.style.transition = 'opacity 0.3s';
            setTimeout(() => el.remove(), 300);
        }, 3000);
    }

    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function setStep(n) {
        STATE.currentStep = n;
        document.querySelectorAll('.step-panel').forEach(p => {
            p.classList.toggle('active', Number(p.dataset.panel) === n);
        });
        document.querySelectorAll('.step-dot').forEach(d => {
            const idx = Number(d.dataset.step);
            d.classList.toggle('active', idx === n);
            d.classList.toggle('completed', idx < n);
        });
        document.querySelectorAll('[data-step-title]').forEach(t => {
            const idx = Number(t.dataset.stepTitle);
            t.classList.toggle('active', idx === n);
        });
        // 滚到顶部
        const main = document.querySelector('.setup-main');
        if (main) main.scrollTop = 0;
        window.scrollTo(0, 0);
    }

    // ──────────────────────────────────────────────
    // Step 1: MySQL 数据库配置
    // ──────────────────────────────────────────────

    function bindStep1() {
        const hostInput = document.getElementById('db-host');
        const portInput = document.getElementById('db-port');
        const userInput = document.getElementById('db-user');
        const pwdInput = document.getElementById('db-password');
        const dbInput = document.getElementById('db-database');
        if (!hostInput) return;
        hostInput.addEventListener('input', e => { STATE.db.host = e.target.value.trim(); STATE.db.tested = false; });
        portInput.addEventListener('input', e => { STATE.db.port = parseInt(e.target.value, 10) || 3306; STATE.db.tested = false; });
        userInput.addEventListener('input', e => { STATE.db.user = e.target.value.trim(); STATE.db.tested = false; });
        pwdInput.addEventListener('input', e => { STATE.db.password = e.target.value; STATE.db.tested = false; });
        dbInput.addEventListener('input', e => { STATE.db.database = e.target.value.trim(); STATE.db.tested = false; });
    }

    async function testDbConnection() {
        const resultEl = document.getElementById('db-test-result');
        if (resultEl) { resultEl.textContent = '⏳ 测试中...'; resultEl.className = 'test-result'; }
        try {
            const r = await api('/setup/database' + tokenQuery(), {
                method: 'POST',
                body: JSON.stringify({
                    host: STATE.db.host,
                    port: STATE.db.port,
                    user: STATE.db.user,
                    password: STATE.db.password,
                    database: STATE.db.database,
                }),
            });
            if (r.status === 'ok') {
                STATE.db.tested = true;
                if (resultEl) { resultEl.textContent = '✅ ' + (r.message || '连接成功'); resultEl.className = 'test-result ok'; }
                showToast('数据库连接成功', 'success');
            } else {
                STATE.db.tested = false;
                if (resultEl) { resultEl.textContent = '❌ ' + (r.message || '连接失败'); resultEl.className = 'test-result fail'; }
            }
        } catch (e) {
            STATE.db.tested = false;
            if (resultEl) { resultEl.textContent = '❌ ' + e.message; resultEl.className = 'test-result fail'; }
        }
    }

    // ──────────────────────────────────────────────
    // Step 2: 管理员账号
    // ──────────────────────────────────────────────

    function bindStep2() {
        const userInput = document.getElementById('admin-username');
        const pwdInput = document.getElementById('admin-password');
        const pwdConfirm = document.getElementById('admin-password-confirm');
        if (!userInput) return;
        userInput.addEventListener('input', e => { STATE.admin.username = e.target.value.trim() || 'admin'; });
        pwdInput.addEventListener('input', e => { STATE.admin.password = e.target.value; });
        pwdConfirm.addEventListener('input', e => { /* 只在提交时验证 */ });
    }

    async function submitAdmin() {
        const pwd = STATE.admin.password || '';
        const confirm = document.getElementById('admin-password-confirm');
        const confirmVal = confirm ? confirm.value : '';
        if (pwd.length < 8) {
            showToast('密码至少 8 位', 'error');
            return false;
        }
        if (confirmVal && pwd !== confirmVal) {
            showToast('两次输入的密码不一致', 'error');
            return false;
        }
        try {
            const r = await api('/setup/admin' + tokenQuery(), {
                method: 'POST',
                body: JSON.stringify({
                    host: STATE.db.host,
                    port: STATE.db.port,
                    user: STATE.db.user,
                    password: STATE.db.password,
                    database: STATE.db.database,
                    admin_username: STATE.admin.username || 'admin',
                    admin_password: pwd,
                }),
            });
            if (r.status === 'ok') {
                showToast('数据库已初始化，管理员已创建', 'success');
                return true;
            } else {
                showToast('初始化失败：' + (r.message || '未知错误'), 'error');
                return false;
            }
        } catch (e) {
            showToast('初始化失败：' + e.message, 'error');
            return false;
        }
    }

    // ──────────────────────────────────────────────
    // Step 3: 平台列表
    // ──────────────────────────────────────────────

    function renderProviderList() {
        const container = document.getElementById('provider-list');
        if (!container) return;
        container.innerHTML = STATE.providers.map(p => {
            const isSet = STATE.currentKeys[p.api_key_name];
            const badgeHtml = p.badge ? `<span class="badge">${escapeHtml(p.badge)}</span>` : '';
            const statusHtml = isSet
                ? `<div class="provider-key-status set">✓ ${escapeHtml(p.api_key_name)} 已配置</div>`
                : '';
            const checked = STATE.selectedProviders.has(p.id) ? 'selected' : '';
            const checkMark = STATE.selectedProviders.has(p.id) ? '✓' : '';
            return `
                <div class="provider-card ${checked}" data-provider="${escapeHtml(p.id)}">
                    <div class="check">${checkMark}</div>
                    <div class="provider-info">
                        <div class="provider-name">
                            ${escapeHtml(p.name)} ${badgeHtml}
                        </div>
                        <div class="provider-desc">${escapeHtml(p.description)}</div>
                        ${statusHtml}
                    </div>
                </div>
            `;
        }).join('');

        container.querySelectorAll('.provider-card').forEach(card => {
            card.addEventListener('click', () => {
                const id = card.dataset.provider;
                if (STATE.selectedProviders.has(id)) {
                    STATE.selectedProviders.delete(id);
                } else {
                    STATE.selectedProviders.add(id);
                }
                renderProviderList();
            });
        });
    }

    async function loadProviderList() {
        if (IS_COLD_START) {
            // 冷启动：调 /setup/api/state（注意：冷启动时该路由是 token 鉴权）
            // 但我们已经在 setup_router 里实现了 /api/state —— 仍走 admin 鉴权，
            // 所以冷启动下我们需要从后端拿一份 provider 列表。
            // 解决：调 /api/providers（setup_router 里挂载的路径，冷启动会 403）。
            // 更稳的方案：让后端在冷启动时也允许 /setup/api/state 用 token 鉴权。
            // 简化：直接用硬编码的 provider 列表（与服务端一致）。
            STATE.providers = HARDCODED_PROVIDERS;
            // 已配置 key 不可知（冷启动阶段 .env 还没写 key），全部未配置
            for (const p of STATE.providers) STATE.currentKeys[p.api_key_name] = false;
            renderProviderList();
            return;
        }
        // 正常启动：拉后端列表
        try {
            const state = await api('/setup/api/state');
            if (state && state.providers) {
                STATE.providers = state.providers.providers || [];
                STATE.currentKeys = state.providers.current_api_keys || {};
                for (const p of STATE.providers) {
                    if (STATE.currentKeys[p.api_key_name]) {
                        STATE.selectedProviders.add(p.id);
                    }
                }
            }
            if (state && state.storage) {
                const s = state.storage;
                if (s.storage_mode === 'cos' || s.storage_mode === 'local') {
                    STATE.storage.storageMode = s.storage_mode;
                }
                if (s.vector_storage_backend === 'numpy' || s.vector_storage_backend === 'cos') {
                    STATE.storage.vectorStorageBackend = s.vector_storage_backend;
                }
                if (s.tencent_cos_bucket) {
                    STATE.storage.tencentCosBucket = s.tencent_cos_bucket;
                }
            }
            renderProviderList();
        } catch (err) {
            console.warn('load state failed', err);
            // fallback：硬编码
            STATE.providers = HARDCODED_PROVIDERS;
            for (const p of STATE.providers) STATE.currentKeys[p.api_key_name] = false;
            renderProviderList();
        }
    }

    // 冷启动用：硬编码的 provider 列表（与服务端 services/setup_service.py:PROVIDER_LIST 保持一致）
    const HARDCODED_PROVIDERS = [
        { id: 'qwen', name: '阿里云百炼', description: '推荐，一个 Key 覆盖文本/视觉/嵌入/搜索', badge: '推荐', api_key_name: 'QWEN_API_KEY', covers: ['text', 'vision', 'embedding', 'search'], models: [], capability_models: { text: ['qwen3.6-flash', 'qwen3.6-plus', 'qwen3.7-plus', 'qwen3.7-max'], vision: ['qwen3.6-35b-a3b', 'qwen3-vl-flash', 'qwen3-vl-plus'], embedding: ['text-embedding-v4'] } },
        { id: 'deepseek', name: 'DeepSeek', description: '中文更自然，有深度思考', badge: null, api_key_name: 'DEEPSEEK_API_KEY', covers: ['text'], models: [], capability_models: { text: ['deepseek-v4-flash'] } },
        { id: 'zhipuai', name: '智谱 GLM', description: 'flash 模型免费，GLM-4.6V 支持视觉', badge: null, api_key_name: 'ZHIPU_API_KEY', covers: ['text', 'vision'], models: [], capability_models: { text: ['glm-4.7', 'glm-4.7-flash', 'glm-4.5-air', 'glm-5-turbo', 'glm-5'], vision: ['glm-4.6v'] } },
        { id: 'kimi', name: 'Kimi (月之暗面)', description: '搜索能力强，长上下文', badge: null, api_key_name: 'KIMI_API_KEY', covers: ['search', 'text'], models: [], capability_models: { text: ['kimi-k2.5', 'kimi-k2.6'] } },
        { id: 'mimo', name: '小米 MiMo', description: '速度快', badge: null, api_key_name: 'MIMO_API_KEY', covers: ['text'], models: [], capability_models: { text: ['mimo-v2.5', 'mimo-v2.5-pro', 'mimo-v2.5-pro-ultraspeed'] } },
        { id: 'doubao', name: '火山引擎 (豆包)', description: '图片理解 / 文生图', badge: null, api_key_name: 'DOUBAO_API_KEY', covers: ['vision', 'image_generation'], models: [], capability_models: { vision: ['doubao-seed-2-0-lite-260215'] } },
    ];

    // ──────────────────────────────────────────────
    // Step 4: 填 Key
    // ──────────────────────────────────────────────

    function renderKeyInputs() {
        const container = document.getElementById('key-inputs');
        if (!container) return;
        const list = STATE.providers.filter(p =>
            STATE.selectedProviders.has(p.id) || STATE.currentKeys[p.api_key_name]
        );
        if (list.length === 0) {
            container.innerHTML = '<div class="info-box"><div class="info-icon">💡</div><div class="info-text">没有选择任何平台，返回上一步勾选至少一个。</div></div>';
            return;
        }
        container.innerHTML = list.map(p => {
            const hasKey = !!STATE.currentKeys[p.api_key_name];
            const value = STATE.keys[p.api_key_name] != null ? STATE.keys[p.api_key_name] : '';
            const status = STATE.verifyResults[p.api_key_name];
            let statusText = '';
            let statusClass = '';
            if (status) {
                if (status.ok) {
                    statusText = '✅ 已验证';
                    statusClass = 'ok';
                } else {
                    statusText = '❌ ' + (status.error || '失败');
                    statusClass = 'fail';
                }
            } else if (hasKey) {
                statusText = '✓ 已存在（输入新值可覆盖）';
                statusClass = 'ok';
            }
            const placeholder = hasKey ? '••••••••' : escapeHtml(p.api_key_name);
            const hasKeyAttr = hasKey ? ' data-has-key="true"' : '';
            return `
                <div class="key-group" data-key="${escapeHtml(p.api_key_name)}">
                    <div class="key-group-header">
                        <div class="key-group-title">${escapeHtml(p.name)}</div>
                        <div class="key-group-status ${statusClass}">${statusText}</div>
                    </div>
                    <div class="key-input-wrapper">
                        <input type="password"
                               class="key-input"
                               data-keyname="${escapeHtml(p.api_key_name)}"${hasKeyAttr}
                               placeholder="${placeholder}"
                               value="${escapeHtml(value)}"
                               autocomplete="off"
                               spellcheck="false">
                        <button type="button" class="key-toggle" data-toggle>显示</button>
                    </div>
                </div>
            `;
        }).join('');

        container.querySelectorAll('.key-input').forEach(input => {
            input.addEventListener('input', e => {
                const name = e.target.dataset.keyname;
                STATE.keys[name] = e.target.value;
                if (STATE.verifyResults[name]) {
                    delete STATE.verifyResults[name];
                    renderKeyInputs();
                }
            });
            input.addEventListener('focus', e => {
                if (e.target.dataset.hasKey === 'true') {
                    e.target.placeholder = e.target.dataset.keyname;
                    delete e.target.dataset.hasKey;
                }
            });
        });
        container.querySelectorAll('[data-toggle]').forEach(btn => {
            btn.addEventListener('click', () => {
                const input = btn.parentElement.querySelector('.key-input');
                if (input.type === 'password') {
                    input.type = 'text';
                    btn.textContent = '隐藏';
                } else {
                    input.type = 'password';
                    btn.textContent = '显示';
                }
            });
        });
    }

    async function submitStep4() {
        const updates = {};
        for (const [k, v] of Object.entries(STATE.keys)) {
            if (v && v.trim()) updates[k] = v.trim();
        }
        if (Object.keys(updates).length === 0) {
            return;
        }
        try {
            await api('/setup/api-keys' + tokenQuery(), {
                method: 'POST',
                body: JSON.stringify({ keys: updates }),
            });
            for (const k of Object.keys(updates)) STATE.currentKeys[k] = true;
            showToast(`已保存 ${Object.keys(updates).length} 个 API key`, 'success');
        } catch (e) {
            showToast('保存失败：' + e.message, 'error');
            throw e;
        }
    }

    async function verifyAll() {
        const list = STATE.providers.filter(p =>
            STATE.selectedProviders.has(p.id) || STATE.currentKeys[p.api_key_name]
        );
        if (list.length === 0) {
            showToast('没有可测试的 Key', 'info');
            return;
        }
        try {
            await submitStep4();
        } catch (e) {
            return;
        }

        const resultsEl = document.getElementById('verify-results');
        resultsEl.innerHTML = list.map(p => `
            <div class="verify-row testing" data-row="${escapeHtml(p.api_key_name)}">
                <span class="icon">⏳</span>
                <span>${escapeHtml(p.name)} — 测试中...</span>
            </div>
        `).join('');

        for (const p of list) {
            try {
                const r = await api('/setup/verify/' + p.id + tokenQuery(), { method: 'POST' });
                STATE.verifyResults[p.api_key_name] = r;
                const row = resultsEl.querySelector(`[data-row="${p.api_key_name}"]`);
                if (row) {
                    row.classList.remove('testing');
                    if (r.ok) {
                        row.classList.add('ok');
                        row.innerHTML = `<span class="icon">✅</span><span>${escapeHtml(p.name)} — 连接成功</span>`;
                    } else {
                        row.classList.add('fail');
                        row.innerHTML = `<span class="icon">❌</span><span>${escapeHtml(p.name)} — ${escapeHtml(r.error || '失败')}</span>`;
                    }
                }
            } catch (e) {
                STATE.verifyResults[p.api_key_name] = { ok: false, error: e.message };
            }
        }
        renderKeyInputs();
    }

    // ──────────────────────────────────────────────
    // Step 5: 选择模型
    // ──────────────────────────────────────────────

    function collectAvailableModels(capability) {
        const out = [];
        for (const p of STATE.providers) {
            if (!STATE.currentKeys[p.api_key_name]) continue;
            const capModels = (p.capability_models || {})[capability];
            if (!capModels || capModels.length === 0) continue;
            for (const m of capModels) {
                out.push({ providerId: p.id, providerName: p.name, model: m });
            }
        }
        return out.length ? out : null;
    }

    function renderCapabilitySelect(capabilityKey, label, options, currentValue) {
        const groups = {};
        for (const opt of options) {
            if (!groups[opt.providerName]) groups[opt.providerName] = [];
            groups[opt.providerName].push(opt.model);
        }
        const optgroups = Object.entries(groups).map(([pname, models]) => `
            <optgroup label="${escapeHtml(pname)}">
                ${models.map(m => {
                    const sel = (m === currentValue) ? 'selected' : '';
                    return `<option value="${escapeHtml(m)}" ${sel}>${escapeHtml(m)}</option>`;
                }).join('')}
            </optgroup>
        `).join('');

        return `
            <div class="summary-row">
                <span class="label">${escapeHtml(label)}</span>
                <span class="value">
                    <select class="setup-select" data-cap="${escapeHtml(capabilityKey)}">
                        ${optgroups}
                    </select>
                </span>
            </div>
        `;
    }

    function renderEmptyCapabilityRow(label) {
        return `
            <div class="summary-row">
                <span class="label">${escapeHtml(label)}</span>
                <span class="value miss">暂无可用的模型</span>
            </div>
        `;
    }

    async function renderSummary() {
        const container = document.getElementById('summary');
        if (!container) return;
        container.innerHTML = '<div class="verify-row testing"><span class="icon">⏳</span><span>检测能力覆盖...</span></div>';

        let verifyData = { results: [], overall_ok: false };
        if (!IS_COLD_START) {
            // 正常启动：调 /setup/verify 看连通性
            try {
                verifyData = await api('/setup/verify' + tokenQuery(), { method: 'POST' });
            } catch (e) {
                console.warn('verify failed', e);
            }
        }

        const textOpts = collectAvailableModels('text');
        const visionOpts = collectAvailableModels('vision');
        const embedOpts = collectAvailableModels('embedding');

        if (!STATE.selectedModels.text && textOpts) {
            STATE.selectedModels.text = textOpts[0].model;
        }
        if (!STATE.selectedModels.vision && visionOpts) {
            STATE.selectedModels.vision = visionOpts[0].model;
        }
        if (!STATE.selectedModels.embedding && embedOpts) {
            STATE.selectedModels.embedding = embedOpts[0].model;
        }

        const searchEngines = [
            { id: 'qwen', keyName: 'QWEN_API_KEY', displayName: 'Qwen3.5-Flash 搜索（qwen）' },
            { id: 'glm',  keyName: 'ZHIPU_API_KEY', displayName: 'GLM-4.7-Flash 搜索（glm）' },
            { id: 'kimi', keyName: 'KIMI_API_KEY',  displayName: 'Kimi k2.6 搜索（kimi）' },
        ];
        const availableEngines = searchEngines.filter(e => STATE.currentKeys[e.keyName]);

        if (availableEngines.length > 0 &&
            !availableEngines.some(e => e.id === STATE.selectedModels.searchEngine)) {
            STATE.selectedModels.searchEngine = availableEngines[0].id;
        }

        const searchEngineOptions = availableEngines.length
            ? availableEngines.map(e => `
                <option value="${escapeHtml(e.id)}" ${STATE.selectedModels.searchEngine === e.id ? 'selected' : ''}>${escapeHtml(e.displayName)}</option>
            `).join('')
            : '<option value="" disabled selected>（暂未配置任何搜索引擎的 API Key）</option>';

        const searchEngineHtml = `
            <div class="summary-row">
                <span class="label">联网搜索后端</span>
                <span class="value">
                    <select class="setup-select" data-cap="searchEngine" ${availableEngines.length === 0 ? 'disabled' : ''}>
                        ${searchEngineOptions}
                    </select>
                </span>
            </div>
        `;

        const connectedProviders = (verifyData.results || [])
            .filter(r => r.ok && r.provider)
            .map(r => r.provider);

        const html = `
            <div class="summary-section">
                <div class="summary-section-title">为每个能力选择默认模型</div>
                ${textOpts ? renderCapabilitySelect('text', '✅ 文本模型', textOpts, STATE.selectedModels.text)
                          : renderEmptyCapabilityRow('⚠️ 文本模型')}
                ${visionOpts ? renderCapabilitySelect('vision', '👁 视觉模型', visionOpts, STATE.selectedModels.vision)
                            : renderEmptyCapabilityRow('👁 视觉模型')}
                ${embedOpts ? renderCapabilitySelect('embedding', '🧬 嵌入模型', embedOpts, STATE.selectedModels.embedding)
                           : renderEmptyCapabilityRow('🧬 嵌入模型')}
                ${searchEngineHtml}
            </div>
            <div class="summary-section">
                <div class="summary-section-title">账户信息</div>
                <div class="summary-row">
                    <span class="label">管理员</span>
                    <span class="value">${escapeHtml(STATE.admin.username || 'admin')}</span>
                </div>
                <div class="summary-row">
                    <span class="label">已连通平台</span>
                    <span class="value">${connectedProviders.length
                        ? connectedProviders.map(escapeHtml).join('、')
                        : '（未测试或全部失败）'}</span>
                </div>
            </div>
        `;
        container.innerHTML = html;

        container.querySelectorAll('select.setup-select').forEach(sel => {
            sel.addEventListener('change', e => {
                const cap = e.target.dataset.cap;
                STATE.selectedModels[cap] = e.target.value;
            });
        });
    }

    // ──────────────────────────────────────────────
    // Step 6: 存储 / 完成
    // ──────────────────────────────────────────────

    function renderRadioOption(name, value, label, desc, checked) {
        return `
            <label class="storage-option ${checked ? 'selected' : ''}" data-radio-name="${escapeHtml(name)}" data-radio-value="${escapeHtml(value)}">
                <input type="radio" name="${escapeHtml(name)}" value="${escapeHtml(value)}" ${checked ? 'checked' : ''}>
                <div class="storage-option-body">
                    <div class="storage-option-label">
                        <span class="storage-radio-dot"></span>
                        ${escapeHtml(label)}
                    </div>
                    <div class="storage-option-desc">${escapeHtml(desc)}</div>
                </div>
            </label>
        `;
    }

    function renderStorageConfig() {
        const container = document.getElementById('storage-config');
        if (!container) return;
        const s = STATE.storage;

        const cosDisabled = s.storageMode !== 'cos';
        const cosPlaceholder = s.tencentCosBucket
            ? s.tencentCosBucket
            : 'firstentrance-gz01-1257148458';

        const html = `
            <div class="storage-section">
                <div class="storage-section-title">文件存储</div>
                ${renderRadioOption(
                    'storage_mode', 'local',
                    '本地文件存储（推荐，零配置）',
                    '存储在服务器本地，无需额外账号。适合个人部署。',
                    s.storageMode === 'local'
                )}
                ${renderRadioOption(
                    'storage_mode', 'cos',
                    '腾讯云对象存储（COS）',
                    '需填写腾讯云 SecretId 和 SecretKey。',
                    s.storageMode === 'cos'
                )}
                <div class="storage-cos-fields" id="storage-cos-fields" ${cosDisabled ? 'hidden' : ''}>
                    <div class="form-group">
                        <label>SecretId</label>
                        <input type="text" class="cos-input" data-cos-field="secret_id" placeholder="AKIDxxxxxxxxxxxxxxxxxxxx" value="${escapeHtml(s.tencentCosSecretId)}" autocomplete="off" spellcheck="false">
                    </div>
                    <div class="form-group">
                        <label>SecretKey</label>
                        <input type="password" class="cos-input" data-cos-field="secret_key" placeholder="••••••••" value="${escapeHtml(s.tencentCosSecretKey)}" autocomplete="off" spellcheck="false">
                    </div>
                    <div class="form-group">
                        <label>Bucket <span class="optional-hint">（可选，默认为 firstentrance-gz01-1257148458）</span></label>
                        <input type="text" class="cos-input" data-cos-field="bucket" placeholder="${escapeHtml(cosPlaceholder)}" value="${escapeHtml(s.tencentCosBucket)}" autocomplete="off" spellcheck="false">
                    </div>
                </div>
            </div>

            <div class="storage-section">
                <div class="storage-section-title">向量搜索</div>
                ${renderRadioOption(
                    'vector_backend', 'numpy',
                    'Numpy 向量搜索（推荐，零依赖）',
                    '本地运行，无需数据库扩展。适合个人部署。',
                    s.vectorStorageBackend === 'numpy'
                )}
                ${renderRadioOption(
                    'vector_backend', 'cos',
                    '腾讯云向量存储（COS VectorBucket）',
                    '需先启用 COS 文件存储，利用同一套存储后端。',
                    s.vectorStorageBackend === 'cos'
                )}
            </div>
        `;
        container.innerHTML = html;

        container.querySelectorAll('.storage-option').forEach(opt => {
            opt.addEventListener('click', e => {
                if (e.target.tagName === 'INPUT') return;
                const name = opt.dataset.radioName;
                const value = opt.dataset.radioValue;
                selectStorageOption(name, value);
            });
            const inp = opt.querySelector('input[type="radio"]');
            if (inp) {
                inp.addEventListener('change', () => {
                    if (inp.checked) selectStorageOption(opt.dataset.radioName, opt.dataset.radioValue);
                });
            }
        });

        container.querySelectorAll('.cos-input').forEach(inp => {
            inp.addEventListener('input', e => {
                const f = e.target.dataset.cosField;
                if (f === 'secret_id') STATE.storage.tencentCosSecretId = e.target.value.trim();
                if (f === 'secret_key') STATE.storage.tencentCosSecretKey = e.target.value.trim();
                if (f === 'bucket') STATE.storage.tencentCosBucket = e.target.value.trim();
            });
        });
    }

    function selectStorageOption(name, value) {
        if (name === 'storage_mode') {
            STATE.storage.storageMode = value;
        } else if (name === 'vector_backend') {
            STATE.storage.vectorStorageBackend = value;
        }
        renderStorageConfig();
    }

    async function submitStorageConfig() {
        const payload = {
            storage_mode: STATE.storage.storageMode,
            tencent_cos_secret_id: STATE.storage.tencentCosSecretId || '',
            tencent_cos_secret_key: STATE.storage.tencentCosSecretKey || '',
            tencent_cos_bucket: STATE.storage.tencentCosBucket || '',
            vector_storage_backend: STATE.storage.vectorStorageBackend,
        };
        await api('/setup/storage' + tokenQuery(), {
            method: 'POST',
            body: JSON.stringify(payload),
        });
    }

    async function finishSetup() {
        // 同步下拉框
        const selects = document.querySelectorAll('#summary select.setup-select');
        selects.forEach(sel => {
            const cap = sel.dataset.cap;
            if (cap && sel.value) STATE.selectedModels[cap] = sel.value;
        });

        const finishBtn = document.getElementById('btn-finish');
        if (finishBtn) finishBtn.disabled = true;

        try {
            await submitStorageConfig();
            await api('/setup/complete' + tokenQuery(), {
                method: 'POST',
                body: JSON.stringify({
                    default_llm_model: STATE.selectedModels.text || '',
                    default_vision_model: STATE.selectedModels.vision || '',
                    default_embedding_model: STATE.selectedModels.embedding || '',
                    default_search_engine: STATE.selectedModels.searchEngine || 'qwen',
                }),
            });
            // 冷启动：显示重启提示
            // 正常启动：跳到 /dashboard
            if (IS_COLD_START) {
                showRestartPrompt();
            } else {
                showToast('配置完成！正在进入控制台...', 'success');
                setTimeout(() => { window.location.href = '/dashboard'; }, 800);
            }
        } catch (e) {
            showToast('完成失败：' + e.message, 'error');
            if (finishBtn) finishBtn.disabled = false;
        }
    }

    function showRestartPrompt() {
        // 把 Step 6 替换为重启提示
        const panel = document.querySelector('[data-panel="6"]');
        if (!panel) {
            // fallback
            alert('配置完成！请重启后端服务（uvicorn / systemctl restart）后使用新设置的管理员账号登录。');
            return;
        }
        panel.innerHTML = `
            <h1>✅ 配置完成！</h1>
            <p class="step-subtitle">所有配置已写入 <code>.env</code>。请重启后端服务后用新账号登录。</p>
            <div class="restart-prompt">
                <div class="restart-section">
                    <div class="restart-section-title">手动重启</div>
                    <pre><code># 方式 1: systemd
sudo systemctl restart feclaw

# 方式 2: 直接 uvicorn
pkill -f uvicorn
python -m uvicorn main:app --host 0.0.0.0 --port 8080</code></pre>
                </div>
                <div class="restart-section">
                    <div class="restart-section-title">重启后登录</div>
                    <p style="color:#aaa;font-size:13px;line-height:1.6;">
                        浏览器打开 <code>http://&lt;host&gt;:&lt;port&gt;/</code> 用您设置的管理员账号登录。
                    </p>
                </div>
            </div>
        `;
        // 把上一个 step-dot 标完成
        document.querySelectorAll('.step-dot').forEach(d => {
            d.classList.remove('active');
            d.classList.add('completed');
        });
    }

    // ──────────────────────────────────────────────
    // 导航
    // ──────────────────────────────────────────────

    async function next() {
        const s = STATE.currentStep;
        if (s === 1) {
            // 校验：必须先测试通过
            if (!STATE.db.tested) {
                showToast('请先点击「测试连接」', 'error');
                return;
            }
            setStep(2);
        } else if (s === 2) {
            // 提交 admin
            const ok = await submitAdmin();
            if (!ok) return;
            // 加载 provider 列表
            await loadProviderList();
            setStep(3);
        } else if (s === 3) {
            if (STATE.selectedProviders.size === 0 &&
                !Object.values(STATE.currentKeys).some(Boolean)) {
                showToast('请至少选择一个平台', 'info');
                return;
            }
            setStep(4);
            renderKeyInputs();
        } else if (s === 4) {
            try {
                await submitStep4();
                setStep(5);
                renderSummary();
            } catch (e) {
                // submitStep4 already toasted
            }
        } else if (s === 5) {
            const selects = document.querySelectorAll('#summary select.setup-select');
            selects.forEach(sel => {
                const cap = sel.dataset.cap;
                if (cap && sel.value) STATE.selectedModels[cap] = sel.value;
            });
            setStep(6);
            renderStorageConfig();
        }
    }

    function prev() {
        if (STATE.currentStep > 1) {
            setStep(STATE.currentStep - 1);
        }
    }

    function bindActions() {
        document.querySelectorAll('[data-action="next"]').forEach(b => {
            b.addEventListener('click', next);
        });
        document.querySelectorAll('[data-action="prev"]').forEach(b => {
            b.addEventListener('click', prev);
        });
        const verifyBtn = document.getElementById('btn-verify');
        if (verifyBtn) verifyBtn.addEventListener('click', verifyAll);
        const finishBtn = document.getElementById('btn-finish');
        if (finishBtn) finishBtn.addEventListener('click', finishSetup);
        const testDbBtn = document.getElementById('btn-test-db');
        if (testDbBtn) testDbBtn.addEventListener('click', testDbConnection);
    }

    // ──────────────────────────────────────────────
    // 入口
    // ──────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', () => {
        // 冷启动模式但无 token：兜底跳到 / 或 /login
        if (document.documentElement.getAttribute('data-no-token') === '1') {
            const guard = document.getElementById('no-token-guard');
            if (guard) guard.style.display = 'flex';
            // 隐藏 setup 主容器
            const container = document.querySelector('.setup-container');
            if (container) container.style.display = 'none';
            // 2 秒后跳到 /login
            setTimeout(() => { window.location.href = '/login'; }, 3000);
            return;
        }
        bindStep1();
        bindStep2();
        bindActions();
    });

    // 暴露给 console 调试
    window.__feclaw_setup__ = { STATE, setStep, next, prev, IS_COLD_START, SETUP_TOKEN };
})();
