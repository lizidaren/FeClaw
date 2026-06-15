/**
 * 统一认证模块
 * 支持就地 TOTP 验证（不跳转）、session 检查、用户信息获取
 *
 * 核心功能：
 * 1. checkAuthBeforeRender() - 阻塞式认证检查，在页面渲染前执行
 * 2. fetch 包装 - 自动添加 Authorization 头，处理 401 认证失败
 */
const Auth = {
  // JWT 存储键名
  JWT_KEY: 'feclaw_jwt',
  USER_KEY: 'feclaw_user',
  EXPIRES_KEY: 'feclaw_jwt_expires_at',

  // 根域名（SSO 同步用）
  ROOT_DOMAIN: 'feclaw.lizidaren.cn',

  // API 端点
  API_BASE: '/api/workspace',
  VERIFY_TOTP_ENDPOINT: '/api/workspace/totp/verify',

  // 登录页面路径
  LOGIN_PATH: '/login',

  /**
   * 在页面加载前检查认证（阻塞式）
   * 必须在 <head> 中调用，阻止页面渲染
   * @returns {Promise<boolean>} 是否已认证
   */
  async checkAuthBeforeRender() {
    // 0. 检查 URL 参数中的 token（SSO 同步回调或 OAuth 回调）
    //    先保存 token，防止后续检查因无 token 而跳转
    const urlParams = new URLSearchParams(window.location.search);
    const urlToken = urlParams.get('token');
    if (urlToken) {
      this.setToken(urlToken);
      // 清理 URL 中的 token，避免泄露
      urlParams.delete('token');
      const newUrl = window.location.pathname + (urlParams.toString() ? '?' + urlParams.toString() : '');
      window.history.replaceState({}, '', newUrl);
    }

    const token = this.getToken();
    const expiresAt = localStorage.getItem(this.EXPIRES_KEY);

    // 1. 没有 JWT → 根据域名决定行为
    if (!token) {
      if (this._isSubdomain()) {
        // 子域名：先尝试 SSO sync（从根域名要 token）
        const currentPath = window.location.pathname + window.location.search;
        const currentHost = window.location.hostname;
        const syncUrl = `https://${this.ROOT_DOMAIN}/api/auth/sync?redirect=${encodeURIComponent(currentPath)}&host=${encodeURIComponent(currentHost)}`;
        window.location.href = syncUrl;
        return false;
      }
      // 根域名：跳转登录页（但不在登录页本身跳转，防止无限循环）
      if (!window.location.pathname.includes('/login')) {
        this.redirectToLogin();
      }
      return false;
    }

    // 2. JWT 已过期 → 清除并跳转登录
    if (expiresAt && Date.now() > parseInt(expiresAt)) {
      this.clearAuth();
      if (!window.location.pathname.includes('/login')) {
        this.redirectToLogin();
      }
      return false;
    }

    // 3. 有 token 且未过期，返回成功
    return true;
  },

  /**
   * 显示登录选项页（TOTP + Platform Login）
   * 在 SSO 静默同步失败后显示
   * @param {string} host 当前子域名
   * @param {string} path 当前路径
   */
  _showLoginOptions(host, path) {
    // 阻止原页面渲染
    document.write('<div style="display:none">');

    const overlay = document.createElement('div');
    overlay.id = 'auth-login-options';
    overlay.innerHTML = `
      <style>
        #auth-login-options {
          position: fixed; top: 0; left: 0; right: 0; bottom: 0;
          background: linear-gradient(135deg, #0f0f23 0%, #1a1a2e 100%);
          display: flex; align-items: center; justify-content: center;
          z-index: 10000; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        }
        .login-options-box {
          background: #1a1a2e; border: 1px solid #2a2a4a;
          border-radius: 20px; padding: 40px; max-width: 420px; width: 90%;
          box-shadow: 0 20px 60px rgba(0,0,0,0.5);
        }
        .login-logo {
          width: 64px; height: 64px;
          background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
          border-radius: 16px; display: flex; align-items: center; justify-content: center;
          font-size: 28px; margin: 0 auto 20px;
        }
        .login-title { font-size: 22px; color: #fff; text-align: center; margin-bottom: 6px; }
        .login-subtitle { font-size: 14px; color: #888; text-align: center; margin-bottom: 28px; }
        .login-divider {
          display: flex; align-items: center; gap: 12px; margin: 24px 0; color: #555; font-size: 13px;
        }
        .login-divider::before,
        .login-divider::after { content: ''; flex: 1; height: 1px; background: #2a2a4a; }
        .totp-input-group { margin-bottom: 16px; }
        .totp-input-group label { display: block; color: #888; font-size: 13px; margin-bottom: 6px; }
        .totp-input-group input {
          width: 100%; padding: 12px 16px;
          background: #0f0f1a; border: 1px solid #2a2a4a;
          border-radius: 8px; color: #fff; font-size: 20px;
          text-align: center; letter-spacing: 6px;
          font-family: 'Monaco', 'Consolas', monospace;
        }
        .totp-input-group input:focus { outline: none; border-color: #667eea; }
        .totp-error { color: #f87171; font-size: 13px; text-align: center; margin-top: 8px; min-height: 20px; }
        .btn { width: 100%; padding: 12px; border: none; border-radius: 8px; cursor: pointer; font-size: 15px; transition: opacity 0.2s; }
        .btn:hover { opacity: 0.9; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .btn-secondary { background: transparent; border: 1px solid #2a2a4a; color: #e0e0e0; display: flex; align-items: center; justify-content: center; gap: 8px; }
        .btn-secondary:hover { border-color: #667eea; }
        .login-sub { color: #555; font-size: 12px; text-align: center; margin-top: 20px; }
        .login-sub a { color: #667eea; text-decoration: none; }
        .login-sub a:hover { text-decoration: underline; }
        .agent-hash-display { text-align: center; margin-bottom: 8px; font-size: 13px; color: #555; }
        .agent-hash-display code { color: #667eea; }
      </style>
      <div class="login-options-box">
        <div class="login-logo">🌊</div>
        <div class="login-title">FeClaw 登录</div>
        <div class="login-subtitle">子域名 <code style="color:#667eea;">${host}</code> 需要认证</div>
        <div class="agent-hash-display">Agent: <code>${this._getAgentHashFromHost(host)}</code></div>

        <button class="btn btn-secondary" onclick="Auth._goToPlatformLogin('${encodeURIComponent(host)}', '${encodeURIComponent(path)}')" style="margin-bottom:16px;">
          🔑 跳转 Platform 登录
        </button>

        <div class="login-divider">或者使用 TOTP 验证码</div>

        <div class="totp-input-group">
          <label>6 位验证码</label>
          <input type="text" id="login-totp-input" placeholder="000000" maxlength="6" autocomplete="off" inputmode="numeric" pattern="[0-9]*">
        </div>
        <div class="totp-error" id="login-totp-error"></div>
        <button class="btn btn-primary" id="login-totp-btn" onclick="Auth._verifyLoginTotp('${path}')">验证并登录</button>

        <div class="login-sub">
          首次使用？<a href="https://feclaw.lizidaren.cn/login">前往 Platform 注册</a>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    // 自动聚焦
    setTimeout(() => {
      const input = document.getElementById('login-totp-input');
      if (input) input.focus();
    }, 100);

    // Enter 键触发验证
    document.getElementById('login-totp-input').addEventListener('keypress', (e) => {
      if (e.key === 'Enter') Auth._verifyLoginTotp(path);
    });
  },

  /**
   * 从 host 中提取 agent hash
   */
  _getAgentHashFromHost(host) {
    const match = host.match(/^([a-f0-9]{4})\./);
    return match ? match[1] : 'unknown';
  },

  /**
   * 跳转 Platform 登录（SSO 同步）
   */
  _goToPlatformLogin(host, path) {
    const syncUrl = `https://${this.ROOT_DOMAIN}/api/auth/sync?redirect=${path}&host=${host}`;
    window.location.href = syncUrl;
  },

  /**
   * 通过 TOTP 验证登录
   */
  async _verifyLoginTotp(redirectPath) {
    const code = document.getElementById('login-totp-input').value;
    const errorEl = document.getElementById('login-totp-error');
    const btn = document.getElementById('login-totp-btn');

    if (code.length !== 6) {
      errorEl.textContent = '请输入 6 位验证码';
      return;
    }

    errorEl.textContent = '';
    btn.disabled = true;
    btn.textContent = '验证中...';

    const host = window.location.hostname;
    const agentHash = this._getAgentHashFromHost(host);

    try {
      const res = await fetch('/api/totp/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_hash: agentHash, code: code })
      });

      if (res.ok) {
        const data = await res.json();
        this.setAuth(data.token, data.expires_at, { agent_hash: data.agent_hash, user_id: data.user_id });
        // 移除遮罩，刷新页面
        document.getElementById('auth-login-options').remove();
        window.location.reload();
      } else {
        const err = await res.json();
        errorEl.textContent = '验证失败: ' + (err.detail || '验证码无效');
        btn.disabled = false;
        btn.textContent = '验证并登录';
      }
    } catch (e) {
      errorEl.textContent = '请求失败: ' + e.message;
      btn.disabled = false;
      btn.textContent = '验证并登录';
    }
  },

  /**
   * 包装 fetch，自动添加 Authorization 头并处理 401
   * 使用方式：直接调用 window.fetch()
   */
  async fetch(url, options = {}) {
    const token = this.getToken();
    if (token) {
      options.headers = options.headers || {};
      if (!(options.headers instanceof Headers)) {
        options.headers = new Headers(options.headers);
      }
      options.headers.set('Authorization', `Bearer ${token}`);
    }

    const response = await window._originalFetch(url, options);

    // 认证失败 → 清除认证并跳转登录
    if (response.status === 401) {
      this.clearAuth();
      this.redirectToLogin();
      throw new Error('Authentication failed');
    }

    return response;
  },

  /**
   * 清除所有认证信息
   */
  clearAuth() {
    localStorage.removeItem(this.JWT_KEY);
    localStorage.removeItem(this.EXPIRES_KEY);
    localStorage.removeItem(this.USER_KEY);
    localStorage.removeItem('feclaw_agent_hash');
    // 清除主 cookie
    document.cookie = 'feclaw_jwt=; path=/; SameSite=Lax; max-age=0';
    document.cookie = 'feclaw_jwt=; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=0';
    // 清除所有 TOTP Agent 专属 cookie
    var cookies = document.cookie.split('; ');
    for (var i = 0; i < cookies.length; i++) {
      var parts = cookies[i].split('=');
      if (parts[0].startsWith('feclaw_jwt_totp_')) {
        document.cookie = parts[0] + '=; path=/; max-age=0';
        document.cookie = parts[0] + '=; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=0';
      }
    }
  },

  /**
   * 设置认证信息（登录成功后调用）
   * @param {string} token JWT token
   * @param {string|number} expiresAt 过期时间（ISO 字串或毫秒数）
   * @param {object} user 用户信息（可选）
   */
  setAuth(token, expiresAt, user = null) {
    this.setToken(token);
    if (expiresAt) {
      // 支持两种格式：ISO 字串或毫秒数
      const expiresMs = typeof expiresAt === 'string'
        ? (expiresAt.includes('T') ? new Date(expiresAt).getTime() : parseInt(expiresAt))
        : expiresAt;
      localStorage.setItem(this.EXPIRES_KEY, expiresMs.toString());
    }
    if (user) {
      this.setUser(user);
    }
  },

  /**
   * 检查登录状态
   * @returns {Promise<{loggedIn: boolean, user?: object}>}
   */
  async checkSession() {
    const token = this.getToken();
    if (!token) {
      return { loggedIn: false };
    }

    // 验证 token 是否有效
    try {
      const response = await fetch(`${this.API_BASE}/me`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });

      if (response.ok) {
        const data = await response.json();
        // 保存用户信息
        this.setUser({ user_id: data.user_id, workspace_root: data.workspace_root });
        return { loggedIn: true, user: data };
      } else if (response.status === 401) {
        // Token 无效，清除
        this.clearToken();
        return { loggedIn: false };
      }
    } catch (error) {
      console.error('[Auth] checkSession error:', error);
    }

    return { loggedIn: false };
  },

  /**
   * 就地验证 TOTP（当前页面验证，不跳转）
   * @param {string} code - 6位验证码
   * @param {string} agentHash - Agent 的 4位 hash（可选，部分场景需要）
   * @returns {Promise<{success: boolean, token?: string, error?: string}>}
   */
  async verifyTotp(code, agentHash = null) {
    try {
      // 构建请求体
      // 注意：现有 API 需要 user_id，但我们可能没有
      // 如果 URL 中有 agent_hash，可以用它来查找 user_id
      const body = { code };

      // 如果提供了 agentHash，加入请求（需要后端支持）
      if (agentHash) {
        body.agent_hash = agentHash;
      }

      const response = await fetch(this.VERIFY_TOTP_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });

      if (response.ok) {
        const data = await response.json();
        // 保存 token
        this.setToken(data.token);
        if (data.user_id) {
          this.setUser({ user_id: data.user_id });
        }
        return { success: true, token: data.token, expires_at: data.expires_at };
      } else {
        const errorData = await response.json().catch(() => ({ detail: '验证失败' }));
        return { success: false, error: errorData.detail || '验证码无效或已过期' };
      }
    } catch (error) {
      console.error('[Auth] verifyTotp error:', error);
      return { success: false, error: '网络错误，请重试' };
    }
  },

  /**
   * 检查 URL 参数中的 TOTP 并验证
   * URL 格式：?totp=XXXXXX 或 ?totp=XXXXXX&agent=ABCD
   * @returns {Promise<{verified: boolean, error?: string}>}
   */
  async checkUrlTotp() {
    const params = new URLSearchParams(window.location.search);
    const totp = params.get('totp');
    const agentHash = params.get('agent') || params.get('agent_hash');

    if (!totp) {
      return { verified: false };
    }

    console.log('[Auth] Found TOTP in URL:', totp, 'agent:', agentHash);

    // 显示验证提示
    this.showVerifyingOverlay();

    try {
      // 先尝试直接验证（如果 API 支持纯 TOTP 验证）
      let result;

      if (agentHash) {
        // 使用 agent_hash + code 验证
        result = await this.verifyWithAgent(agentHash, totp);
      } else {
        // 尝试使用现有的 verify API（可能需要 user_id）
        // 这里我们假设后端可能扩展支持纯 code 验证
        result = await this.verifyTotp(totp);
      }

      if (result.success) {
        // 清除 URL 参数中的 TOTP（避免重复验证）
        this.clearUrlParams(['totp', 'agent', 'agent_hash']);

        // 隐藏验证提示
        this.hideVerifyingOverlay();

        return { verified: true };
      } else {
        this.hideVerifyingOverlay();
        this.showError(result.error || '验证失败');

        return { verified: false, error: result.error };
      }
    } catch (error) {
      this.hideVerifyingOverlay();
      console.error('[Auth] checkUrlTotp error:', error);
      return { verified: false, error: '验证过程出错' };
    }
  },

  /**
   * 使用 Agent hash + TOTP 验证
   * （调用 Agent TOTP 验证 API）
   * @param {string} agentHash - Agent 的 4位 hash
   * @param {string} code - 6位验证码
   * @returns {Promise<{success: boolean, token?: string, error?: string}>}
   */
  async verifyWithAgent(agentHash, code) {
    try {
      // 使用统一的简化路径端点 /api/totp/verify
      const response = await fetch('/api/totp/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_hash: agentHash, code })
      });

      if (response.ok) {
        const data = await response.json();
        this.setToken(data.token);
        this.setUser({ user_id: data.user_id, agent_hash: data.agent_hash });
        return { success: true, token: data.token };
      } else {
        // 如果该 API 不存在，尝试备用方式
        const errorData = await response.json().catch(() => ({ detail: '验证失败' }));
        return { success: false, error: errorData.detail };
      }
    } catch (error) {
      console.error('[Auth] verifyWithAgent error:', error);
      return { success: false, error: '网络错误' };
    }
  },

  /**
   * 跳转到登录页
   */
  redirectToLogin() {
    window.location.href = this.LOGIN_PATH;
  },

  /**
   * 获取当前用户信息
   * @returns {Promise<{user_id?: string, workspace_root?: string} | null>}
   */
  async getCurrentUser() {
    // 先从本地存储获取
    const cachedUser = this.getUser();
    if (cachedUser && cachedUser.user_id) {
      return cachedUser;
    }

    // 如果有 token，从 API 获取
    const { loggedIn, user } = await this.checkSession();
    if (loggedIn) {
      return user;
    }

    return null;
  },

  /**
   * 确保已登录（用于页面初始化）
   * 流程：检查 session → 如果未登录且 URL 有 TOTP → 验证 → 如果验证失败 → 跳转登录
   * @returns {Promise<boolean>} 是否已登录
   */
  async ensureAuthenticated() {
    // 1. 检查现有 session
    const { loggedIn } = await this.checkSession();
    if (loggedIn) {
      return true;
    }

    // 2. 检查 URL 参数中的 TOTP
    const { verified } = await this.checkUrlTotp();
    if (verified) {
      return true;
    }

    // 3. 未登录且无有效 TOTP，跳转登录
    this.redirectToLogin();
    return false;
  },

  // ========== Token 管理 ==========

  getToken() {
    let token = localStorage.getItem(this.JWT_KEY);
    if (!token) {
      token = this.getTokenFromCookie();
      if (token) {
        localStorage.setItem(this.JWT_KEY, token);
      }
    }
    return token;
  },

  getTokenFromCookie() {
    const match = document.cookie.match(new RegExp('(^| )feclaw_jwt=([^;]+)'));
    return match ? decodeURIComponent(match[2]) : null;
  },

  setToken(token) {
    localStorage.setItem(this.JWT_KEY, token);
    // 同时设置 cookie，供服务端页面路由认证使用
    document.cookie = `feclaw_jwt=${token}; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=${60*60*24*7}`;
  },

  clearToken() {
    localStorage.removeItem(this.JWT_KEY);
    localStorage.removeItem(this.USER_KEY);
    // 清除 cookie
    document.cookie = 'feclaw_jwt=; path=/; SameSite=Lax; max-age=0';
    document.cookie = 'feclaw_jwt=; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=0';
  },

  // ========== User 信息管理 ==========

  getUser() {
    try {
      const userStr = localStorage.getItem(this.USER_KEY);
      return userStr ? JSON.parse(userStr) : null;
    } catch {
      return null;
    }
  },

  setUser(user) {
    localStorage.setItem(this.USER_KEY, JSON.stringify(user));
  },

  // ========== URL 参数清理 ==========

  clearUrlParams(paramsToRemove) {
    const url = new URL(window.location.href);
    paramsToRemove.forEach(param => url.searchParams.delete(param));
    window.history.replaceState({}, '', url.toString());
  },

  // ========== UI 辅助 ==========

  showVerifyingOverlay() {
    if (document.getElementById('auth-verifying-overlay')) return;

    const overlay = document.createElement('div');
    overlay.id = 'auth-verifying-overlay';
    overlay.innerHTML = `
      <style>
        #auth-verifying-overlay {
          position: fixed;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(0, 0, 0, 0.7);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 10000;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        }
        .auth-verifying-box {
          background: #1a1a2e;
          border-radius: 16px;
          padding: 30px 40px;
          text-align: center;
          color: #e0e0e0;
        }
        .auth-spinner {
          width: 40px;
          height: 40px;
          border: 3px solid rgba(102, 126, 234, 0.3);
          border-top-color: #667eea;
          border-radius: 50%;
          animation: auth-spin 1s linear infinite;
          margin: 0 auto 15px;
        }
        @keyframes auth-spin {
          to { transform: rotate(360deg); }
        }
        .auth-verifying-text {
          font-size: 16px;
        }
      </style>
      <div class="auth-verifying-box">
        <div class="auth-spinner"></div>
        <div class="auth-verifying-text">正在验证登录...</div>
      </div>
    `;
    document.body.appendChild(overlay);
  },

  hideVerifyingOverlay() {
    const overlay = document.getElementById('auth-verifying-overlay');
    if (overlay) overlay.remove();
  },

  showError(message) {
    const existingError = document.getElementById('auth-error-modal');
    if (existingError) existingError.remove();

    const modal = document.createElement('div');
    modal.id = 'auth-error-modal';
    modal.innerHTML = `
      <style>
        #auth-error-modal {
          position: fixed;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(0, 0, 0, 0.7);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 10000;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        }
        .auth-error-box {
          background: #1a1a2e;
          border-radius: 16px;
          padding: 30px 40px;
          text-align: center;
          color: #e0e0e0;
          max-width: 400px;
        }
        .auth-error-icon {
          font-size: 48px;
          margin-bottom: 15px;
        }
        .auth-error-message {
          font-size: 16px;
          margin-bottom: 20px;
          color: #ff6b6b;
        }
        .auth-error-btn {
          background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
          color: white;
          border: none;
          padding: 12px 30px;
          border-radius: 8px;
          cursor: pointer;
          font-size: 15px;
        }
        .auth-error-btn:hover {
          opacity: 0.9;
        }
      </style>
      <div class="auth-error-box">
        <div class="auth-error-icon">⚠️</div>
        <div class="auth-error-message">${message}</div>
        <button class="auth-error-btn" onclick="Auth.goToLogin()">去登录</button>
      </div>
    `;
    document.body.appendChild(modal);
  },

  /**
   * 判断当前页面是否在子域名上
   * @returns {boolean}
   */
  _isSubdomain() {
    const host = window.location.hostname;
    // 根域名本身 → 不是子域名
    if (host === this.ROOT_DOMAIN || host === 'localhost' || host === '127.0.0.1') {
      return false;
    }
    // 匹配 *.feclaw.lizidaren.cn → 子域名
    if (host.endsWith('.' + this.ROOT_DOMAIN)) {
      return true;
    }
    return false;
  },

  goToLogin() {
    document.getElementById('auth-error-modal')?.remove();
    this.redirectToLogin();
  }
};

// ========== 初始化 fetch 包装 ==========

// 保存原始 fetch，并替换为 Auth.fetch
// 注意：只在非登录页执行，避免登录页的 fetch 被拦截导致无法登录
if (!window._originalFetch && !window.location.pathname.includes('/login')) {
  window._originalFetch = window.fetch;
  window.fetch = Auth.fetch.bind(Auth);
}

// 导出（如果使用模块系统）
if (typeof module !== 'undefined' && module.exports) {
  module.exports = Auth;
}