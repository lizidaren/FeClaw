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
            const value = STATE.keys[p.api_key_name] || '';
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
            } else if (STATE.currentKeys[p.api_key_name]) {
                statusText = '✓ 已存在';
                statusClass = 'ok';
            }
            return `
                <div class="key-group" data-key="${escapeHtml(p.api_key_name)}">
                    <div class="key-group-header">
                        <div class="key-group-title">${escapeHtml(p.name)}</div>
                        <div class="key-group-status ${statusClass}">${statusText}</div>
                    </div>
                    <div class="key-input-wrapper">
                        <input type="password"
                               class="key-input"
                               data-keyname="${escapeHtml(p.api_key_name)}"
                               placeholder="${escapeHtml(p.api_key_name)}"
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

    async function renderSummary() {
        const container = document.getElementById('summary');
        if (!container) return;
        container.innerHTML = '<div class="verify-row testing"><span class="icon">⏳</span><span>检测能力覆盖...</span></div>';

        let verifyData = { results: [], overall_ok: false };
        try {
            verifyData = await api('/setup/verify', { method: 'POST' });
        } catch (e) {
            console.warn('verify failed', e);
        }

        const hasText = STATE.providers.some(p =>
            p.covers.includes('text') && STATE.currentKeys[p.api_key_name]
        );
        const hasVision = STATE.providers.some(p =>
            p.covers.includes('vision') && STATE.currentKeys[p.api_key_name]
        );
        const hasEmbedding = STATE.providers.some(p =>
            p.covers.includes('embedding') && STATE.currentKeys[p.api_key_name]
        );
        const hasSearch = STATE.providers.some(p =>
            p.covers.includes('search') && STATE.currentKeys[p.api_key_name]
        );

        const providerFor = (cap) => {
            const p = STATE.providers.find(pp =>
                pp.covers.includes(cap) && STATE.currentKeys[pp.api_key_name]
            );
            return p ? `${p.models[0] || '?'} (${p.name})` : '未配置';
        };

        container.innerHTML = `
            <div class="summary-section">
                <div class="summary-section-title">能力覆盖</div>
                <div class="summary-row">
                    <span class="label">✅ 文本模型</span>
                    <span class="value ${hasText ? 'ok' : 'miss'}">${escapeHtml(hasText ? providerFor('text') : '未配置')}</span>
                </div>
                <div class="summary-row">
                    <span class="label">${hasVision ? '✅' : '⚠️'} 视觉模型</span>
                    <span class="value ${hasVision ? 'ok' : 'miss'}">${escapeHtml(hasVision ? providerFor('vision') : '未配置')}</span>
                </div>
                <div class="summary-row">
                    <span class="label">${hasEmbedding ? '✅' : '⚠️'} 嵌入模型</span>
                    <span class="value ${hasEmbedding ? 'ok' : 'miss'}">${escapeHtml(hasEmbedding ? providerFor('embedding') : '未配置')}</span>
                </div>
                <div class="summary-row">
                    <span class="label">${hasSearch ? '✅' : '⚠️'} 联网搜索</span>
                    <span class="value ${hasSearch ? 'ok' : 'miss'}">${escapeHtml(hasSearch ? providerFor('search') : '未配置')}</span>
                </div>
            </div>
            <div class="summary-section">
                <div class="summary-section-title">建议</div>
                <div class="summary-row">
                    <span class="label">建议主模型</span>
                    <span class="value">${escapeHtml(hasText ? providerFor('text').split(' ')[0] : 'deepseek-v4-flash')}</span>
                </div>
                <div class="summary-row">
                    <span class="label">管理员</span>
                    <span class="value">admin ${STATE.admin.email ? '· ' + escapeHtml(STATE.admin.email) : ''}</span>
                </div>
            </div>
        `;
    }

    async function finishSetup() {
        try {
            await api('/setup/complete', { method: 'POST' });
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
