/* token-sync.js —— 把 SSO 的 cookie 兜进 localStorage，并摘掉「假 Bearer」
 *
 * 背景（2026-10-02）：
 *   Platform SSO 登录按 P0-2 的约定，**只把 JWT 写进 cookie**（不进 URL，这是对的）；
 *   但页面里大量老代码只从 localStorage 读 token：
 *       let jwt = localStorage.getItem('feclaw_jwt');  ... 'Authorization': `Bearer ${jwt}`
 *   读不到就发成 `Authorization: Bearer null`；而后端取 token 的顺序是
 *   「Bearer header 优先，其次 feclaw_jwt cookie」⇒ 空 Bearer **把本来能用的 cookie 盖掉**
 *   ⇒ 控制台接口 401、dashboard 显示「用户」。
 *
 * 本文件做两件事（幂等、无副作用、不跳转）：
 *   ① cookie → localStorage 同步：cookie 有值就写进 localStorage（cookie 是活的会话，以它为准）
 *   ② 包一层 fetch：Authorization 是 Bearer null / undefined / 空 → 删掉这个头，
 *      让后端自然回落到 cookie 认证
 */
(function () {
  var KEY = 'feclaw_jwt';

  // ① cookie → localStorage
  try {
    var m = document.cookie.match(/(?:^|;\s*)feclaw_jwt=([^;]+)/);
    var token = m ? decodeURIComponent(m[1]) : null;
    if (token && localStorage.getItem(KEY) !== token) {
      localStorage.setItem(KEY, token);
    }
  } catch (e) { /* 隐私模式 / 存储禁用：忽略 */ }

  // ② 摘掉「假 Bearer」
  function isFake(v) {
    if (v === null || v === undefined) return true;
    var s = String(v).trim();
    return s === '' || /^Bearer\s*(null|undefined)?$/i.test(s);
  }
  function dropFake(h) {
    try {
      if (!h) return;
      if (typeof Headers !== 'undefined' && h instanceof Headers) {
        if (h.has('Authorization') && isFake(h.get('Authorization'))) h.delete('Authorization');
      } else if (Array.isArray(h)) {
        for (var i = h.length - 1; i >= 0; i--) {
          if (String(h[i] && h[i][0]).toLowerCase() === 'authorization' && isFake(h[i][1])) h.splice(i, 1);
        }
      } else if (typeof h === 'object') {
        Object.keys(h).forEach(function (k) {
          if (k.toLowerCase() === 'authorization' && isFake(h[k])) delete h[k];
        });
      }
    } catch (e) {}
  }
  if (typeof window.fetch === 'function' && !window.__feclawTokenSync) {
    window.__feclawTokenSync = true;
    var orig = window.fetch;
    window.fetch = function (input, init) {
      try {
        if (init && init.headers) dropFake(init.headers);
        else if (input && typeof input === 'object' && input.headers) dropFake(input.headers);
      } catch (e) {}
      return orig.apply(this, arguments);
    };
  }
})();
