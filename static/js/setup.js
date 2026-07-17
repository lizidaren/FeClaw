/* FeClaw 配置向导 — 前端交互逻辑
 *
 * 状态机：currentStep (1..4) + 各步骤的内存数据
 * 数据收集完成后，每个 step 在 next 时通过 fetch 写入 /setup/api/...
 *
 * 鉴权：localStorage.feclaw_jwt —— 与 utils/auth_dependencies 约定的 cookie 同源。
 * 若 token 缺失，提示用户登录。
 */

(function () {
    'use strict';

    const STATE = {
        currentStep: 1,
        admin: { email: '' },
        selectedProviders: new Set(),
        keys: {},            // {QWEN_API_KEY: '...'}
        verifyResults: {},   // {QWEN_API_KEY: {ok: true}}
        providers: [],       // 从 /setup/api/providers 拉取
        currentKeys: {},     // {QWEN_API_KEY: true/false}
        // Step 4 用户在每个能力下拉框中选定的模型（提交时发到后端）。
        // text / vision / embedding: 模型名字符串；空串表示该能力未配置。
        // searchEngine: qwen | glm | kimi，独立于模型。
        selectedModels: { text: '', vision: '', embedding: '', searchEngine: 'qwen' },
    };

    // ──────────────────────────────────────────────
    // 工具
    // ──────────────────────────────────────────────

    function getToken() {
        return localStorage.getItem('feclaw_jwt') || '';
    }

    async function api(path, opts = {}) {
        const headers = Object.assign(
            { 'Content-Type': 'application/json' },
            opts.headers || {}
        );
        const token = getToken();
        if (token) headers['Authorization'] = 'Bearer ' + token;

        const resp = await fetch(path, Object.assign({}, opts, { headers }));
        if (resp.status === 401) {
            showToast('登录已过期，请重新登录', 'error');
            setTimeout(() => { window.location.href = '/login'; }, 1500);
            throw new Error('unauthorized');
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
    }

    // ──────────────────────────────────────────────
    // Step 1: 管理员邮箱
    // ──────────────────────────────────────────────

    function loadAdminEmail() {
        api('/setup/api/state').then(state => {
            if (state && state.admin) {
                STATE.admin.username = state.admin.username;
            }
            if (state && state.providers) {
                STATE.providers = state.providers.providers || [];
                STATE.currentKeys = state.providers.current_api_keys || {};
                // 已配置 Key 的 provider 自动勾选 —— 用户进 Step 2 时无需再手动选一次。
                // Set 自带去重，重复 add 无副作用。
                for (const p of STATE.providers) {
                    if (STATE.currentKeys[p.api_key_name]) {
                        STATE.selectedProviders.add(p.id);
                    }
                }
                renderProviderList();
            }
        }).catch(err => {
            console.warn('load state failed', err);
        });
    }

    function bindStep1() {
        const emailInput = document.getElementById('admin-email');
        if (emailInput) {
            emailInput.addEventListener('input', e => {
                STATE.admin.email = e.target.value.trim();
            });
        }
    }

    async function submitStep1() {
        // 邮箱可空 —— 跳过就不发请求
        if (!STATE.admin.email) return;
        try {
            await api('/setup/admin', {
                method: 'POST',
                body: JSON.stringify({ email: STATE.admin.email, password: '' }),
            });
        } catch (e) {
            console.warn('update admin email failed', e);
        }
    }

    // ──────────────────────────────────────────────
    // Step 2: 平台列表
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

        // 绑定点击
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

    // ──────────────────────────────────────────────
    // Step 3: 填 Key
    // ──────────────────────────────────────────────

    function renderKeyInputs() {
        const container = document.getElementById('key-inputs');
        if (!container) return;
        // 只显示已勾选 + 未配置过的 platform
        const list = STATE.providers.filter(p =>
            STATE.selectedProviders.has(p.id) || STATE.currentKeys[p.api_key_name]
        );
        if (list.length === 0) {
            container.innerHTML = '<div class="info-box"><div class="info-icon">💡</div><div class="info-text">没有选择任何平台，返回上一步勾选至少一个。</div></div>';
            return;
        }
        container.innerHTML = list.map(p => {
            const hasKey = !!STATE.currentKeys[p.api_key_name];
            // 用户已开始输入的值优先；否则空（占位符由 hasKey 决定）
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
            // 已配置 Key 时：placeholder 显示 8 个点，value 留空，加 data-has-key。
            // 用户聚焦/点击时由下面绑定的 focus 事件清空 placeholder + 移除 data-has-key。
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

        // 绑定 input + 切换可见
        container.querySelectorAll('.key-input').forEach(input => {
            input.addEventListener('input', e => {
                const name = e.target.dataset.keyname;
                STATE.keys[name] = e.target.value;
                // 清掉旧 verify 状态
                if (STATE.verifyResults[name]) {
                    delete STATE.verifyResults[name];
                    renderKeyInputs();
                }
            });
            // 已配置 Key 的 input：聚焦时清掉占位符 + 移除 data-has-key，表示用户准备覆盖。
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

    async function submitStep3() {
        // 收集非空 key
        const updates = {};
        for (const [k, v] of Object.entries(STATE.keys)) {
            if (v && v.trim()) updates[k] = v.trim();
        }
        if (Object.keys(updates).length === 0) {
            return; // 无更新
        }
        try {
            await api('/setup/api-keys', {
                method: 'POST',
                body: JSON.stringify({ keys: updates }),
            });
            // 更新 currentKeys
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
        // 先把当前输入的 key 写入 .env，再测试
        try {
            await submitStep3();
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
                const r = await api(`/setup/verify/${p.id}`, { method: 'POST' });
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
    // Step 4: 完成
    // ──────────────────────────────────────────────

    // ──────────────────────────────────────────────
    // Step 4: 完成
    // ──────────────────────────────────────────────

    /**
     * 找出"已配置 + 支持指定能力"的所有 provider，对每个能力聚合出
     *  [{provider, model}, ...] 列表（按 provider 出现顺序）。
     * 返回 null 表示该能力没有任何可用选项。
     */
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

    /**
     * 渲染单个能力的下拉框 HTML。
     * - options: collectAvailableModels 的结果（数组）或 null（无可用）
     * - capabilityKey: 'text' | 'vision' | 'embedding'
     * - currentValue: STATE.selectedModels[capabilityKey]
     */
    function renderCapabilitySelect(capabilityKey, label, options, currentValue) {
        // 按 provider 分组：使用 <optgroup>
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

        // 保留 verify 调用 —— 用于让用户看到"哪个 provider 已连通"。
        // 即便 verify 失败，模型下拉框仍可正常渲染（前端只看 currentKeys）。
        let verifyData = { results: [], overall_ok: false };
        try {
            verifyData = await api('/setup/verify', { method: 'POST' });
        } catch (e) {
            console.warn('verify failed', e);
        }

        // 收集每个能力的可用模型
        const textOpts = collectAvailableModels('text');
        const visionOpts = collectAvailableModels('vision');
        const embedOpts = collectAvailableModels('embedding');

        // 智能默认：未指定时，取每个能力的第一项。
        // 注意：只有该能力当前 selectedModels 为空时才覆盖 —— 这样切换 Provider 后保留选择。
        if (!STATE.selectedModels.text && textOpts) {
            STATE.selectedModels.text = textOpts[0].model;
        }
        if (!STATE.selectedModels.vision && visionOpts) {
            STATE.selectedModels.vision = visionOpts[0].model;
        }
        if (!STATE.selectedModels.embedding && embedOpts) {
            STATE.selectedModels.embedding = embedOpts[0].model;
        }

        // 搜索后端 dropdown 只显示已配置了对应 API Key 的引擎
        // engine -> { keyName, displayName }；按声明顺序输出
        const searchEngines = [
            { id: 'qwen', keyName: 'QWEN_API_KEY', displayName: 'Qwen3.5-Flash 搜索（qwen）' },
            { id: 'glm',  keyName: 'ZHIPU_API_KEY', displayName: 'GLM-4.7-Flash 搜索（glm）' },
            { id: 'kimi', keyName: 'KIMI_API_KEY',  displayName: 'Kimi k2.6 搜索（kimi）' },
        ];
        const availableEngines = searchEngines.filter(e => STATE.currentKeys[e.keyName]);

        // 若当前选中的 searchEngine 未配置，自动降级到第一个可用引擎
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

        // 已连通的 provider 列表（仅展示用，从 verifyData 计算）
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
                    <span class="value">admin ${STATE.admin.email ? '· ' + escapeHtml(STATE.admin.email) : ''}</span>
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

        // 绑定 change：同步到 STATE.selectedModels
        container.querySelectorAll('select.setup-select').forEach(sel => {
            sel.addEventListener('change', e => {
                const cap = e.target.dataset.cap;
                STATE.selectedModels[cap] = e.target.value;
            });
        });
    }

    async function finishSetup() {
        // 强制把下拉框的当前值同步到 STATE（防止 change 事件因任何原因未触发）
        const selects = document.querySelectorAll('#summary select.setup-select');
        selects.forEach(sel => {
            const cap = sel.dataset.cap;
            if (cap && sel.value) STATE.selectedModels[cap] = sel.value;
        });

        try {
            await api('/setup/complete', {
                method: 'POST',
                body: JSON.stringify({
                    default_llm_model: STATE.selectedModels.text || '',
                    default_vision_model: STATE.selectedModels.vision || '',
                    default_embedding_model: STATE.selectedModels.embedding || '',
                    default_search_engine: STATE.selectedModels.searchEngine || 'qwen',
                }),
            });
            showToast('配置完成！正在进入控制台...', 'success');
            setTimeout(() => { window.location.href = '/dashboard'; }, 800);
        } catch (e) {
            showToast('完成失败：' + e.message, 'error');
        }
    }

    // ──────────────────────────────────────────────
    // 导航
    // ──────────────────────────────────────────────

    async function next() {
        if (STATE.currentStep === 1) {
            await submitStep1();
            setStep(2);
        } else if (STATE.currentStep === 2) {
            if (STATE.selectedProviders.size === 0 &&
                !Object.values(STATE.currentKeys).some(Boolean)) {
                showToast('请至少选择一个平台', 'info');
                return;
            }
            setStep(3);
            renderKeyInputs();
        } else if (STATE.currentStep === 3) {
            try {
                await submitStep3();
                setStep(4);
                renderSummary();
            } catch (e) {
                // submitStep3 already toasted
            }
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
    }

    // ──────────────────────────────────────────────
    // 入口
    // ──────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', () => {
        if (!getToken()) {
            showToast('请先登录', 'info');
            setTimeout(() => { window.location.href = '/login'; }, 1200);
            return;
        }
        bindStep1();
        bindActions();
        loadAdminEmail();
    });

    // 暴露给 console 调试
    window.__feclaw_setup__ = { STATE, setStep, next, prev };
})();
