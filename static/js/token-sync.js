/* token-sync.js —— 浏览器认证迁移的**传输层适配**（T18a）
 *
 * 这一版**不再**做 cookie → localStorage 同步（那是把 HttpOnly 会话令牌复制到
 * 脚本可读的存储里，等于把「XSS 偷不到 token」的收益丢掉）。现在的约定是：
 *
 *   会话令牌只在服务端 HttpOnly cookie 里；页面脚本**不持有**它。
 *
 * 本文件仍做两件事（幂等、无副作用、不跳转）：
 *
 * ① 摘掉「假 Bearer」—— 老页面还在写
 *      fetch(url, {headers: {Authorization: 'Bearer ' + localStorage.getItem('feclaw_jwt')}})
 *    迁移期这个值是 null ⇒ 发出 `Authorization: Bearer null`，而服务端取 token 时
 *    Bearer 优先，于是**把本来有效的 cookie 盖掉**。这里把这种假头删掉，
 *    让服务端自然回落到 cookie 认证。
 *
 * ② 给同源 mutation 自动补 `X-CSRF-Token` —— 双提交 token 的浏览器半边。
 *    token 从可读 cookie `feclaw_csrf` 取（服务端登录/`/api/oauth/me` 下发）。
 *    cookie 还没下发时，先做一次同源 `GET /api/oauth/me` 让服务端补发
 *    （该端点在有会话时才会 set-cookie），然后继续原请求。
 *
 * 明确不做：
 *   - 不写 `document.cookie`（服务端 HttpOnly 才是权威）
 *   - 不把 token 写 localStorage
 *   - 不给**跨源**请求加任何认证头 / 不改变 credentials（跨源默认不发 cookie）
 */
(function () {
  'use strict';

  var CSRF_COOKIE = 'feclaw_csrf';
  var CSRF_HEADER = 'X-CSRF-Token';
  var SAFE_METHODS = { GET: 1, HEAD: 1, OPTIONS: 1, TRACE: 1 };
  var LEGACY_KEY = 'feclaw_jwt';

  /** 读一个 cookie（只对非 HttpOnly 的有效；`feclaw_csrf` 就是非 HttpOnly）。 */
  function readCookie(name) {
    try {
      var m = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]+)'));
      return m ? decodeURIComponent(m[1]) : null;
    } catch (e) {
      return null;
    }
  }

  function methodOf(input, init) {
    var m = (init && init.method) || (input && input.method) || 'GET';
    return String(m).toUpperCase();
  }

  function urlOf(input) {
    try {
      var raw = (typeof input === 'string') ? input : (input && input.url) || '';
      return new URL(raw, window.location.href);
    } catch (e) {
      return null;
    }
  }

  function isSameOrigin(input) {
    var u = urlOf(input);
    return !!u && u.origin === window.location.origin;
  }

  // ── ① 摘掉「假 Bearer」─────────────────────────────────────────────
  function isFake(v) {
    if (v === null || v === undefined) return true;
    var s = String(v).trim();
    return s === '' || /^Bearer\s*(null|undefined)?$/i.test(s);
  }

  function dropFake(headers) {
    try {
      if (!headers) return;
      if (typeof Headers !== 'undefined' && headers instanceof Headers) {
        if (headers.has('Authorization') && isFake(headers.get('Authorization'))) {
          headers.delete('Authorization');
        }
      } else if (Array.isArray(headers)) {
        for (var i = headers.length - 1; i >= 0; i--) {
          if (String(headers[i] && headers[i][0]).toLowerCase() === 'authorization' &&
              isFake(headers[i][1])) {
            headers.splice(i, 1);
          }
        }
      } else if (typeof headers === 'object') {
        Object.keys(headers).forEach(function (k) {
          if (k.toLowerCase() === 'authorization' && isFake(headers[k])) delete headers[k];
        });
      }
    } catch (e) { /* 忽略：不能因为清理头失败而让请求失败 */ }
  }

  // ── ② 补 CSRF 头 ──────────────────────────────────────────────────
  function hasCsrfHeader(headers) {
    try {
      if (!headers) return false;
      if (typeof Headers !== 'undefined' && headers instanceof Headers) return headers.has(CSRF_HEADER);
      if (Array.isArray(headers)) {
        return headers.some(function (h) {
          return String(h && h[0]).toLowerCase() === CSRF_HEADER.toLowerCase() && h[1];
        });
      }
      if (typeof headers === 'object') {
        return Object.keys(headers).some(function (k) {
          return k.toLowerCase() === CSRF_HEADER.toLowerCase() && headers[k];
        });
      }
    } catch (e) {}
    return false;
  }

  function setCsrfHeader(init, token) {
    if (!token) return;
    try {
      if (!init.headers) init.headers = {};
      var h = init.headers;
      if (typeof Headers !== 'undefined' && h instanceof Headers) {
        h.set(CSRF_HEADER, token);
      } else if (Array.isArray(h)) {
        h.push([CSRF_HEADER, token]);
      } else if (typeof h === 'object') {
        h[CSRF_HEADER] = token;
      }
    } catch (e) {}
  }

  /** 服务端补发 CSRF cookie 的唯一探测点（有会话才 set-cookie）。 */
  function probeCsrfCookie(rawFetch) {
    try {
      return rawFetch('/api/oauth/me', {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store'
      }).catch(function () { /* 未登录/无会话：忽略 */ });
    } catch (e) {
      return Promise.resolve();
    }
  }

  // ── 安装 fetch 包装（只装一次）────────────────────────────────────
  if (typeof window.fetch === 'function' && !window.__feclawTokenSync) {
    window.__feclawTokenSync = true;
    // 迁移标记：页面可据此知道「会话在 cookie 里」而不再检查 localStorage token
    window.__feclawCookieAuth = true;

    var rawFetch = window.fetch;

    // 供其它脚本/页面复用（同一实现，避免第二套 CSRF 逻辑）
    window.__feclawFetchWithCsrf = function (input, init) {
      init = init || {};
      var needsCsrf = !SAFE_METHODS[methodOf(input, init)] && isSameOrigin(input) && !hasCsrfHeader(init.headers);
      if (!needsCsrf) return rawFetch.call(window, input, init);

      var token = readCookie(CSRF_COOKIE);
      if (token) {
        setCsrfHeader(init, token);
        return rawFetch.call(window, input, init);
      }
      return probeCsrfCookie(rawFetch).then(function () {
        setCsrfHeader(init, readCookie(CSRF_COOKIE));
        return rawFetch.call(window, input, init);
      });
    };

    window.fetch = function (input, init) {
      try {
        if (init && init.headers) dropFake(init.headers);
        else if (input && typeof input === 'object' && input.headers) dropFake(input.headers);
      } catch (e) {}
      return window.__feclawFetchWithCsrf(input, init);
    };
  }

  // ── 迁移提示：localStorage 里的旧 token 不再被本文件复制/续期 ─────────
  // 故意**不**删除它：还有页面（dashboard / agent_chat 等，属 T18b 切换范围）
  // 用 `if (!localStorage.getItem('feclaw_jwt')) → /login` 做守卫，
  // 现在清掉会让这些页面在 cookie 会话有效时也把人踢回登录页。
  // T18b 完成模板切换后再统一清理（见 T18a 报告的跨界请求）。
  try {
    if (readCookie(LEGACY_KEY)) { /* 老版本可能写过非 HttpOnly cookie，不做处理 */ }
  } catch (e) {}
})();
