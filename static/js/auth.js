/**
 * 统一认证模块（T18a：浏览器认证已迁移到服务端 HttpOnly Cookie）
 *
 * 核心变化（相对旧版）：
 * 1. **不再把 JWT 写进 localStorage**，也不再从 cookie 复制 token 到 localStorage。
 *    会话令牌只在 HttpOnly cookie 里 —— 脚本读不到 ⇒ XSS 拿不到会话。
 * 2. 认证判据是**服务端探测**（`GET /api/oauth/me`，cookie 自动携带），
 *    而不是「localStorage 里有没有字符串」。
 * 3. `Auth.fetch` 只给**同源**请求带 cookie（`credentials: 'same-origin'`），
 *    只给**显式移动端 token** 加 `Authorization: Bearer`；跨源一律不带凭据。
 * 4. URL 里的一次性 token（SSO 中继 `#token=`）交给服务端 `/api/user/session`
 *    换成 HttpOnly cookie，然后立刻从地址栏抹掉，**不落 localStorage**。
 * 5. 迁移只读兼容：旧 localStorage token 会被读取一次、换成 cookie、然后删除。
 * 6. 登录后跳转走 `Auth.safeInternalPath`（与后端
 *    `utils.auth_dependencies.safe_internal_redirect_path` 同一套规则）。
 */
const Auth = {
  // 旧存储键名（只用于迁移清理，不再写入）
  JWT_KEY: 'feclaw_jwt',
  USER_KEY: 'feclaw_user',
  EXPIRES_KEY: 'feclaw_jwt_expires_at',
  MOBILE_TOKEN_KEY: 'feclaw_mobile_token',

  // 根域名（SSO 同步用）
  ROOT_DOMAIN: window.ROOT_DOMAIN || '',

  // API 端点
  API_BASE: '/api/workspace',
  VERIFY_TOTP_ENDPOINT: '/api/workspace/totp/verify',
  SESSION_PROBE_ENDPOINT: '/api/oauth/me',
  ADOPT_ENDPOINT: '/api/user/session',
  CSRF_COOKIE: 'feclaw_csrf',

  // 登录页面路径
  LOGIN_PATH: '/login',

  // 迁移期内存中的一次性 token（不写任何持久化存储）
  _memoryToken: null,
  _csrfToken: null,

  /**
   * 在页面加载前检查认证（阻塞式）
   * 必须在 <head> 中调用，阻止页面渲染
   * @returns {Promise<boolean>} 是否已认证
   */
  async checkAuthBeforeRender() {
    // 0. URL 一次性 token（SSO 中继 / 老回调）：换成 cookie，不落 localStorage
    await this._consumeUrlToken();

    // 1. 服务端 cookie 会话是否有效 —— 唯一判据
    if (await this._hasCookieSession()) {
      return true;
    }

    // 2. 迁移兼容：旧的 localStorage token 只读一次 → 换 cookie → 删除
    if (await this._migrateLegacyToken()) {
      return true;
    }

    // 3. 未登录 → 子域走 SSO 中继，根域跳登录页
    if (this._isSubdomain()) {
      const currentPath = window.location.pathname + window.location.search;
      const currentHost = window.location.hostname;
      const syncUrl = `https://${this.ROOT_DOMAIN}/api/auth/sync?redirect=${encodeURIComponent(currentPath)}&host=${encodeURIComponent(currentHost)}`;
      window.location.href = syncUrl;
      return false;
    }
    if (!window.location.pathname.includes('/login')) {
      this.redirectToLogin();
    }
    return false;
  },

  /**
   * 探测服务端 cookie 会话（不抛错、不触发登录跳转）。
   * @returns {Promise<boolean>}
   */
  async _hasCookieSession() {
    const fetcher = window._originalFetch || window.__feclawFetchWithCsrf || window.fetch;
    try {
      const res = await fetcher(this.SESSION_PROBE_ENDPOINT, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store'
      });
      return !!res && res.ok;
    } catch (e) {
      return false;
    }
  },

  /**
   * 消费 URL 中的一次性 token：`?token=`（旧）或 `#token=`（SSO 中继）。
   * 读了立刻从地址栏抹掉（防 Referer / 历史记录 / 截图泄漏），再交给服务端换 cookie。
   */
  async _consumeUrlToken() {
    let token = null;
    let cleaned = null;
    try {
      const params = new URLSearchParams(window.location.search);
      if (params.has('token')) {
        token = params.get('token');
        params.delete('token');
        cleaned = window.location.pathname + (params.toString() ? '?' + params.toString() : '');
      } else if (window.location.hash && window.location.hash.indexOf('#token=') === 0) {
        token = window.location.hash.substring('#token='.length);
        cleaned = window.location.pathname + window.location.search;
      }
    } catch (e) {
      return;
    }
    if (!token) return;

    if (cleaned !== null) {
      try { window.history.replaceState({}, '', cleaned); } catch (e) {}
    }

    const ok = await this.adoptSessionToken(token);
    if (!ok) {
      // 旧 cookie 可能是无效的「同名不同域」变体，挡住新的 cookie 写入。
      // 先做一次幂等登出（服务端会清 host-only + domain 变体），再重试一次。
      try {
        const fetcher = window._originalFetch || window.fetch;
        await fetcher('/api/auth/logout', { method: 'POST', credentials: 'same-origin', keepalive: true });
      } catch (e) {}
      await this.adoptSessionToken(token);
    }
  },

  /**
   * 迁移兼容：读取旧 localStorage token，交给服务端换 HttpOnly cookie，然后删除。
   * 只读一次、只尝试一次；失败即删除（无效 token 留着只会制造登录循环）。
   * @returns {Promise<boolean>}
   */
  async _migrateLegacyToken() {
    let legacy = null;
    try { legacy = localStorage.getItem(this.JWT_KEY); } catch (e) { return false; }
    if (!legacy) return false;

    const ok = await this.adoptSessionToken(legacy);
    if (ok) {
      this._purgeLegacyToken();
      return true;
    }
    this._purgeLegacyToken();
    return false;
  },

  /** 删除旧版 token 存储键（真正迁到 cookie 之后才调用）。 */
  _purgeLegacyToken() {
    try {
      localStorage.removeItem(this.JWT_KEY);
      localStorage.removeItem(this.EXPIRES_KEY);
      localStorage.removeItem('feclaw_jwt_expires');
    } catch (e) {}
    this._memoryToken = null;
  },

  /**
   * 把一次性 token 交给服务端换成 HttpOnly 会话 cookie（不再持久化 token 本身）。
   * @param {string} token
   * @returns {Promise<boolean>}
   */
  async adoptSessionToken(token) {
    if (!token || typeof token !== 'string') return false;
    const fetcher = window._originalFetch || window.fetch;
    try {
      const res = await fetcher(this.ADOPT_ENDPOINT, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token })
      });
      if (!res || !res.ok) return false;
      try {
        const data = await res.json();
        if (data && data.csrf_token) this._csrfToken = data.csrf_token;
      } catch (e) {}
      return true;
    } catch (e) {
      return false;
    }
  },

  /**
   * 读 CSRF token（双提交）：内存优先，其次可读 cookie。
   * 后端下发点：登录 / 注册 / 改密 / `/api/oauth/me`（有会话时补发）。
   * @returns {string|null}
   */
  csrfToken() {
    if (this._csrfToken) return this._csrfToken;
    try {
      const m = document.cookie.match(new RegExp('(?:^|;\\s*)' + this.CSRF_COOKIE + '=([^;]+)'));
      return m ? decodeURIComponent(m[1]) : null;
    } catch (e) {
      return null;
    }
  },

  /**
   * 显式移动端 token 支持（`Authorization: Bearer`），**只用于非浏览器场景**。
   * 浏览器流程不需要、也不应该调用它。
   */
  setMobileToken(token) {
    try {
      if (token) sessionStorage.setItem(this.MOBILE_TOKEN_KEY, token);
      else sessionStorage.removeItem(this.MOBILE_TOKEN_KEY);
    } catch (e) {}
  },

  getMobileToken() {
    try { return sessionStorage.getItem(this.MOBILE_TOKEN_KEY) || null; } catch (e) { return null; }
  },

  /**
   * 内部规范相对路径白名单（**必须**与后端
   * `utils.auth_dependencies.safe_internal_redirect_path` 规则一致）
   * @param {*} value
   * @param {string} fallback
   * @returns {string}
   */
  safeInternalPath(value, fallback) {
    const def = fallback || '/dashboard';
    if (typeof value !== 'string') return def;
    const text = value.trim();
    if (!text) return def;

    const check = (s) => {
      if (!s || s.charAt(0) !== '/') return false;
      if (s.slice(0, 2) === '//' || s.slice(0, 2) === '/\\') return false;
      if (s.indexOf('\\') !== -1) return false;
      for (let i = 0; i < s.length; i++) {
        const c = s.charCodeAt(i);
        if (c < 0x20 || c === 0x7f || c === 0x20) return false;
      }
      const low = s.toLowerCase();
      if (low.indexOf('javascript:') !== -1 || low.indexOf('data:') !== -1 ||
          low.indexOf('vbscript:') !== -1 || low.indexOf('file:') !== -1) return false;
      return true;
    };

    if (!check(text)) return def;
    let decoded = text;
    for (let i = 0; i < 3; i++) {
      let next;
      try { next = decodeURIComponent(decoded); } catch (e) { next = decoded; }
      if (next === decoded) break;
      decoded = next;
    }
    if (decoded !== text && !check(decoded)) return def;
    return text;
  },

  /** 登录成功后按白名单跳转（`data.redirect` / `?redirect_to=`）。 */
  redirectAfterLogin(data) {
    let target = (data && data.redirect) || null;
    if (!target) {
      try {
        target = new URLSearchParams(window.location.search).get('redirect_to');
      } catch (e) { target = null; }
    }
    window.location.href = this.safeInternalPath(target, '/dashboard');
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
        :focus-visible { outline: 2px solid #667eea !important; outline-offset: 2px !important; }
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
          <label for="login-totp-input">6 位验证码</label>
          <input type="text" id="login-totp-input" placeholder="000000" maxlength="6" autocomplete="off" inputmode="numeric" pattern="[0-9]*" aria-label="6 位验证码">
        </div>
        <div class="totp-error" id="login-totp-error" role="alert"></div>
        <button class="btn btn-primary" id="login-totp-btn" onclick="Auth._verifyLoginTotp('${path}')">验证并登录</button>

        <div class="login-sub">
          首次使用？<a href="/login">前往 Platform 注册</a>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    // 自动聚焦
    setTimeout(() => {
      const input = document.getElementById('login-totp-input');
      if (input) input.focus();
    }, 100);

    // Enter 键触发验证（中文输入法选词回车不触发）
    document.getElementById('login-totp-input').addEventListener('keypress', (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter') Auth._verifyLoginTotp(path);
    });
  },

  /**
   * 从 host 解析 agent hash（全局唯一实现，勿在别处再写正则）
   *
   * 统一口径：**4–8 位 hex**。
   *   - 老 agent 为 4 位（如 5178）
   *   - `services/agent_init_service.create_agent` 新建为 8 位（如 692eaa5e）
   * 后端 `utils/agent_access.extract_hash_from_host` 口径与此一致。
   *
   * @param {string} host - window.location.hostname / host（可带端口）
   * @returns {string|null} 小写 hash；不匹配返回 null
   */
  parseAgentHash(host) {
    if (!host) return null;
    const match = String(host).match(/^([a-f0-9]{4,8})\./i);
    return match ? match[1].toLowerCase() : null;
  },

  /**
   * 从 host 中提取 agent hash（兼容旧调用，内部走 parseAgentHash）
   */
  _getAgentHashFromHost(host) {
    return this.parseAgentHash(host) || 'unknown';
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
   * 是否同源（同源才允许带 cookie / Bearer）
   */
  _isSameOrigin(url) {
    try {
      const resolved = new URL(typeof url === 'string' ? url : (url && url.url) || '', window.location.href);
      return resolved.origin === window.location.origin;
    } catch (e) {
      return false;
    }
  },

  /**
   * 包装 fetch：同源带 cookie、跨源不带任何凭据、401 统一处理
   *
   * T18a 规则：
   * - 同源 → `credentials: 'same-origin'`（cookie 自动携带，HttpOnly 也生效）
   * - 跨源 → `credentials: 'omit'` + **绝不**加 `Authorization`
   *   （旧实现无条件加 Bearer，跨源请求会把令牌送到外域）
   * - `Authorization` 只用于**显式移动端 token**（`Auth.setMobileToken` /
   *   `options.mobileToken`），普通浏览器流程不需要它
   *
   * 使用方式：直接调用 window.fetch()（本模块安装为全局包装）
   */
  async fetch(url, options = {}) {
    const fetcher = window._originalFetch || window.__feclawFetchWithCsrf || window.fetch;
    options = options || {};
    const sameOrigin = this._isSameOrigin(url);

    if (sameOrigin) {
      if (!('credentials' in options)) options.credentials = 'same-origin';
    } else {
      // 跨源：不发 cookie、不带 Bearer
      if (!('credentials' in options)) options.credentials = 'omit';
    }

    const explicitMobileToken = options.mobileToken || null;
    if ('mobileToken' in options) delete options.mobileToken;
    const mobileToken = explicitMobileToken || this.getMobileToken();

    if (sameOrigin && mobileToken) {
      options.headers = options.headers || {};
      if (!(options.headers instanceof Headers)) {
        options.headers = new Headers(options.headers);
      }
      options.headers.set('Authorization', `Bearer ${mobileToken}`);
    }

    const response = await fetcher(url, options);

    // 认证失败 → 清理本地状态并跳转登录
    // serverRevoke:false —— 401 可能只是本浏览器会话陈旧，不该连带吊销其他设备
    if (response.status === 401) {
      const path = (window.location && window.location.pathname) || '';
      const isAuthEndpoint = typeof url === 'string' &&
        (url.indexOf('/api/user/login') === 0 ||
         url.indexOf('/api/user/session') === 0 ||
         url.indexOf('/api/oauth/me') === 0);
      if (!path.includes('/login') && !isAuthEndpoint) {
        this.clearAuth({ serverRevoke: false });
        this.redirectToLogin();
      }
      throw new Error('Authentication failed');
    }

    return response;
  },

  /**
   * 按名字删除认证 cookie —— 覆盖「带 domain」和「host-only」两种变体
   *
   * cookie 身份 = name + domain + path（RFC 6265）：登录时 oauth.py 写的是
   * `.ROOT_DOMAIN` 跨子域名 cookie（如 .feclaw.lizidaren.cn），
   * 本地登录/旧代码写的是 host-only —— 两者是不同的 cookie，必须各删一次，
   * 否则会出现「登出后 cookie 还在、旧 token 还能用」的假退出。
   */
  _clearCookie(name) {
    var domains = [''];
    if (this.ROOT_DOMAIN) {
      domains.push('; domain=.' + this.ROOT_DOMAIN);
      domains.push('; domain=' + this.ROOT_DOMAIN);
    }
    for (var i = 0; i < domains.length; i++) {
      document.cookie = name + '=; path=/' + domains[i] + '; SameSite=Lax; max-age=0';
    }
  },

  /**
   * 清除所有认证信息
   * @param {object} options
   *   - redirectToLogout: 是否重定向到 Platform OIDC end_session
   *   - serverRevoke: 是否调用后端吊销旧 token（默认 true）。
   *     被动清理（token 过期、401）请显式传 false —— 否则会把该用户
   *     其他设备的 token 一起吊销（jwt_version 是按用户而非按会话计数的）。
   * @returns {Promise} 后端登出请求的 promise（供调用方在跳转前 await）
   */
  clearAuth(options = {}) {
    this._memoryToken = null;
    this._csrfToken = null;
    localStorage.removeItem(this.JWT_KEY);
    localStorage.removeItem(this.EXPIRES_KEY);
    localStorage.removeItem('feclaw_jwt_expires');
    localStorage.removeItem(this.USER_KEY);
    localStorage.removeItem('feclaw_agent_hash');
    try { sessionStorage.removeItem(this.MOBILE_TOKEN_KEY); } catch (e) {}
    // 清除**非 HttpOnly** 旧 cookie（host-only + domain 变体）。
    // T18a 起会话 cookie 是 HttpOnly，只能由服务端删 —— `/api/auth/logout`
    // （routers/feclaw_domain.py）与 `/api/oauth/logout` 都会清 host-only + domain
    // 两个变体，并附带清 TOTP cookie 与 CSRF cookie。
    this._clearCookie('feclaw_jwt');
    this._clearCookie('feclaw_id_token');
    this._clearCookie('platform_token');
    this._clearCookie(this.CSRF_COOKIE);
    // 清除所有 TOTP Agent 专属 cookie
    var cookies = document.cookie.split('; ');
    for (var i = 0; i < cookies.length; i++) {
      var parts = cookies[i].split('=');
      if (parts[0].startsWith('feclaw_jwt_totp_')) {
        this._clearCookie(parts[0]);
      }
    }

    // 服务端吊销：jwt_version +1 ⇒ 旧 token 立刻失效。
    // keepalive:true 保证紧跟的页面跳转不会把请求取消掉。
    var pending = Promise.resolve();
    if (options.serverRevoke !== false) {
      pending = fetch('/api/auth/logout', { method: 'POST', keepalive: true, credentials: 'same-origin' })
        .catch(function() { /* 网络失败不阻塞登出 */ });
    }

    if (options.redirectToLogout) {
      // 调用后端获取 OIDC end_session 跳转地址
      return pending.then(function() {
        return fetch('/api/oauth/logout', { method: 'POST' })
          .then(function(res) { return res.json(); })
          .then(function(data) {
            window.location.href = data.redirect_url || this.LOGIN_PATH;
          }.bind(this))
          .catch(function() {
            window.location.href = this.LOGIN_PATH;
          }.bind(this));
      }.bind(this));
    }
    return pending;
  },

  /**
   * 设置认证信息（登录成功后调用）
   *
   * T18a：**不再写 localStorage**。会话令牌由服务端 HttpOnly cookie 承载；
   * 这里只把一次性 token 交给服务端换 cookie（`adoptSessionToken`），
   * 并记住非敏感的过期时间（仅用于 UI 提示）。
   *
   * @param {string} token JWT token（一次性交接用；可为 null）
   * @param {string|number} expiresAt 过期时间（ISO 字串或毫秒数）
   * @param {object} user 用户信息（可选，非敏感展示数据）
   */
  setAuth(token, expiresAt, user = null) {
    if (token) {
      this._memoryToken = token;
      // 服务端通常已经种好 cookie；这一调用只覆盖「token 是从别处拿到」的情况
      this.adoptSessionToken(token);
    }
    if (expiresAt) {
      const expiresMs = typeof expiresAt === 'string'
        ? (expiresAt.includes('T') ? new Date(expiresAt).getTime() : parseInt(expiresAt))
        : expiresAt;
      // 只存过期时间（非凭据）—— 方便 UI 提前续期提示
      try { localStorage.setItem(this.EXPIRES_KEY, String(expiresMs)); } catch (e) {}
    }
    if (user) {
      this.setUser(user);
    }
  },

  /**
   * 检查登录状态（T18a：服务端 cookie 探测，不再看 localStorage token）
   * @returns {Promise<{loggedIn: boolean, user?: object}>}
   */
  async checkSession() {
    const fetcher = window._originalFetch || window.fetch;
    try {
      const response = await fetcher(this.SESSION_PROBE_ENDPOINT, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store'
      });
      if (response.ok) {
        const data = await response.json().catch(() => ({}));
        const u = (data && data.user) || {};
        // 只缓存非敏感展示信息（不是凭据）
        this.setUser({ user_id: u.id, username: u.username });
        return { loggedIn: true, user: data };
      }
      if (response.status === 401) {
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
   * @param {string} agentHash - Agent 的 4–8 位 hash（可选，部分场景需要）
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
   * @param {string} agentHash - Agent 的 4–8 位 hash
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

  // ========== Token 管理（T18a：只读兼容，不再写入）==========

  /**
   * 读旧版 localStorage token（**只读兼容**，一个版本后移除）
   *
   * 新登录流程不再产生 localStorage token；这里只为迁移期让尚未切换的页面
   * 仍然「有个值可用」（最终它们也应该只依赖 cookie）。
   */
  getToken() {
    if (this._memoryToken) return this._memoryToken;
    try {
      return localStorage.getItem(this.JWT_KEY) || null;
    } catch (e) {
      return null;
    }
  },

  /** 读非 HttpOnly 的旧 cookie（只读兼容；HttpOnly 会话 cookie 读不到，这是设计）。 */
  getTokenFromCookie() {
    const match = document.cookie.match(new RegExp('(^| )feclaw_jwt=([^;]+)'));
    return match ? decodeURIComponent(match[2]) : null;
  },

  /**
   * 登记 token（T18a：**不写 localStorage / 不写 document.cookie**）
   *
   * 服务端 HttpOnly cookie 才是会话权威；本方法只把一次性 token 交给服务端
   * 换 cookie（`adoptSessionToken`），调用方无需 await。
   */
  setToken(token) {
    if (!token) return;
    this._memoryToken = token;
    // 服务端若已种 cookie，这次调用是幂等的；否则它会把 token 落成 HttpOnly cookie
    this.adoptSessionToken(token);
  },

  clearToken() {
    this._memoryToken = null;
    try {
      localStorage.removeItem(this.JWT_KEY);
      localStorage.removeItem(this.USER_KEY);
      localStorage.removeItem(this.EXPIRES_KEY);
    } catch (e) {}
    // 清掉迁移期可能存在的**非 HttpOnly** 旧 cookie（host-only 与 domain 变体）。
    // HttpOnly 会话 cookie 只能由服务端删（`/api/auth/logout` 已统一处理）。
    this._clearCookie('feclaw_jwt');
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
    // 匹配子域名（如 5178.domain.com）
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