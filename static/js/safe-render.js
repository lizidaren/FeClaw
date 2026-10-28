/*!
 * FeClawSafeRender v1.0.0 — FeClaw 唯一安全渲染边界（规格 T16）
 *
 * 模型输出、群聊消息、persona 预览、文件预览、配置助手全部走这里：
 *   marked.parse(text)  →  DOMPurify.sanitize(html)  →  （可选）KaTeX 数学渲染
 *
 * 依赖（全部自托管，运行时不得访问 CDN）：
 *   - /static/marked.min.js           固定 marked v15.0.12（现有文件，勿升级）
 *   - /static/vendor/dompurify.min.js DOMPurify 3.2.4，逐字节自托管
 *                                     SHA256 8eb41b658831fab175fad9bcd00fcb2d84e0ed3a25a55053d4ecd4444b8b43a0
 *   - /static/katex.min.js            可选；缺失时公式保留为原文
 *
 * 安全策略（fail-closed）：
 *   - 禁用 script/iframe/object/embed/form/input 等主动内容标签；
 *   - 移除全部 on* 事件属性；
 *   - 链接仅放行 http/https/mailto，并强制 target=_blank + rel="noopener noreferrer"；
 *     图片 src 禁止 javascript:/data:/vbscript: 等主动协议（相对路径与 http(s) 保留）；
 *   - marked 或 DOMPurify 任一不可用时，退化为纯文本（escape），绝不退回未消毒 innerHTML。
 *
 * 兼容：本文件不依赖构建工具，普通 <script> 引入后暴露 window.FeClawSafeRender。
 */
(function (global) {
    'use strict';

    var VERSION = '1.0.0';

    // 上游 DOMPurify 3.2.4 dist/purify.min.js 的 SHA256（供测试与审计对账）
    var DOMPURIFY_SHA256 = '8eb41b658831fab175fad9bcd00fcb2d84e0ed3a25a55053d4ecd4444b8b43a0';

    // 主动内容标签：一律禁止（内联执行、表单提交、外部资源替换）
    var FORBID_TAGS = [
        'script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet',
        'form', 'input', 'button', 'select', 'option', 'optgroup', 'textarea',
        'link', 'meta', 'base', 'template', 'svg', 'math',
        'video', 'audio', 'source', 'track', 'portal',
        'marquee', 'plaintext', 'xmp', 'noscript'
    ];

    // 危险属性：即使出现在允许标签上也一并移除
    var FORBID_ATTR = [
        'srcdoc', 'formaction', 'xlink:href', 'action', 'ping', 'background'
    ];

    // DOMPurify 配置：默认 HTML profile（不含 SVG/MathML），再叠加显式禁单
    var PURIFY_CONFIG = {
        USE_PROFILES: { html: true },
        FORBID_TAGS: FORBID_TAGS,
        FORBID_ATTR: FORBID_ATTR,
        ALLOW_DATA_ATTR: true,
        ALLOW_ARIA_ATTR: true,
        KEEP_CONTENT: true,
        // 不做 {{ }} 模板剥离：本应用为服务端 Jinja 一次性渲染，客户端不再二次编译模板，
        // 开启会把代码示例里的 {{var}} 吃掉（内容损失而非安全收益）。
        SAFE_FOR_TEMPLATES: false,
        WHOLE_DOCUMENT: false,
        RETURN_TRUSTED_TYPE: false,
        RETURN_DOM: false,
        RETURN_DOM_FRAGMENT: false,
        SANITIZE_DOM: true,
        IN_PLACE: false
    };

    // 链接白名单：仅绝对 http/https/mailto
    var SAFE_LINK_RE = /^(?:https?:\/\/|mailto:)/i;
    // 图片 src：显式禁止主动协议；允许 http(s)、协议相对、相对路径
    var UNSAFE_SRC_RE = /^\s*(?:javascript|vbscript|data|blob|filesystem)\s*:/i;

    var MATH_RE = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$([^$\n]+?)\$/g;

    // 文本遍历时跳过的容器（代码/公式内部不做公式与提及替换）
    var SKIP_TEXT_TAGS = {
        pre: 1, code: 1, kbd: 1, samp: 1, script: 1, style: 1, textarea: 1, option: 1
    };

    var ESCAPE_MAP = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
        '`': '&#96;'
    };

    var ESCAPE_RE = /[&<>"'`]/g;

    function escapeHtml(value) {
        var s = (value === null || value === undefined) ? '' : String(value);
        return s.replace(ESCAPE_RE, function (ch) { return ESCAPE_MAP[ch]; });
    }

    function trimControlChars(value) {
        // 与浏览器 URL 解析前的空白/控制字符剥离行为保持一致，避免 "java\tscript:" 绕过
        return String(value === null || value === undefined ? '' : value)
            .replace(/[\u0000-\u0020\u007f-\u009f]/g, '');
    }

    function getPurifier() {
        var p = global.DOMPurify;
        if (!p || typeof p.sanitize !== 'function') return null;
        installHooks(p);
        return p;
    }

    function getMarked() {
        var m = global.marked;
        if (!m || typeof m.parse !== 'function') return null;
        return m;
    }

    function hasKatex() {
        var k = global.katex;
        return !!(k && typeof k.renderToString === 'function');
    }

    // ---------------------------------------------------------------- 一次性 hook

    /**
     * 安装 DOMPurify 钩子（幂等：同一 DOMPurify 实例只装一次）。
     * 钩子只改属性、不删节点 —— 在钩子里删节点会让遍历跳过后续兄弟节点。
     */
    function installHooks(purifier) {
        var p = purifier || global.DOMPurify;
        if (!p || typeof p.addHook !== 'function') return false;
        if (p.__feclawSafeRenderHooks === VERSION) return false;
        try {
            p.addHook('afterSanitizeAttributes', function (node) {
                if (!node || node.nodeType !== 1) return;
                stripHandlerAttributes(node);
                var tag = String(node.nodeName || '').toUpperCase();
                if (tag === 'A') hardenLink(node);
                else if (tag === 'IMG') hardenImage(node);
            });
            p.__feclawSafeRenderHooks = VERSION;
            return true;
        } catch (e) {
            return false;
        }
    }

    function stripHandlerAttributes(node) {
        if (!node.attributes) return;
        for (var i = node.attributes.length - 1; i >= 0; i--) {
            var attr = node.attributes[i];
            if (!attr) continue;
            var name = String(attr.name || '').toLowerCase();
            if (name.indexOf('on') === 0) {
                node.removeAttribute(attr.name);
            }
        }
    }

    function hardenLink(node) {
        var href = node.getAttribute('href');
        if (href === null || !SAFE_LINK_RE.test(trimControlChars(href))) {
            // 协议不在白名单：去掉可导航属性，保留可见文本
            node.removeAttribute('href');
            node.removeAttribute('target');
            node.removeAttribute('ping');
            node.setAttribute('rel', 'noopener noreferrer nofollow');
            return;
        }
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
        node.setAttribute('referrerpolicy', 'no-referrer');
    }

    function hardenImage(node) {
        var src = node.getAttribute('src');
        if (src === null || UNSAFE_SRC_RE.test(trimControlChars(src))) {
            node.removeAttribute('src');
            node.removeAttribute('srcset');
            if (!node.getAttribute('alt')) node.setAttribute('alt', '[图片已拦截]');
            return;
        }
        if (node.getAttribute('alt') === null) node.setAttribute('alt', '');
        // 不加载外部跟踪像素时也不做网络请求控制，交由页面 CSP/网络策略处理
    }

    // ---------------------------------------------------------------- 文本节点遍历

    function collectTextNodes(root, skipLinks) {
        var out = [];
        if (!root) return out;
        var walker = global.document.createTreeWalker(root, 4 /* SHOW_TEXT */, {
            acceptNode: function (node) {
                var p = node.parentNode;
                while (p && p.nodeType === 1) {
                    var tag = String(p.nodeName || '').toLowerCase();
                    if (SKIP_TEXT_TAGS[tag]) return 2 /* FILTER_REJECT */;
                    if (p.classList && (p.classList.contains('katex') || p.classList.contains('katex-display'))) {
                        return 2;
                    }
                    if (skipLinks && tag === 'a') return 2;
                    p = p.parentNode;
                }
                return 1 /* FILTER_ACCEPT */;
            }
        }, false);
        var n;
        while ((n = walker.nextNode())) out.push(n);
        return out;
    }

    function replaceMathInNode(node) {
        var text = node.nodeValue;
        if (!text) return;
        var hasDollar = text.indexOf('$') !== -1;
        var hasBracket = text.indexOf('\\(') !== -1 || text.indexOf('\\[') !== -1;
        if (!hasDollar && !hasBracket) return;

        var frag = global.document.createDocumentFragment();
        var last = 0;
        var matched = false;
        var m;
        MATH_RE.lastIndex = 0;
        while ((m = MATH_RE.exec(text)) !== null) {
            var display = (m[1] !== undefined) || (m[2] !== undefined);
            var expr = m[1] !== undefined ? m[1]
                : (m[2] !== undefined ? m[2]
                    : (m[3] !== undefined ? m[3] : m[4]));
            var rendered = null;
            try {
                rendered = global.katex.renderToString(expr, {
                    displayMode: display,
                    throwOnError: false,
                    strict: 'ignore'
                });
            } catch (e) {
                rendered = null;
            }
            if (typeof rendered !== 'string' || rendered === '') continue;

            matched = true;
            if (m.index > last) frag.appendChild(global.document.createTextNode(text.slice(last, m.index)));
            var holder = global.document.createElement('span');
            // KaTeX 输出由本进程根据已转义表达式生成，不是模型输出，不需要再过消毒器
            holder.innerHTML = rendered;
            while (holder.firstChild) frag.appendChild(holder.firstChild);
            last = m.index + m[0].length;
        }
        if (!matched) return;
        if (last < text.length) frag.appendChild(global.document.createTextNode(text.slice(last)));
        if (node.parentNode) node.parentNode.replaceChild(frag, node);
    }

    function renderMathIn(container) {
        if (!hasKatex()) return;
        var nodes = collectTextNodes(container, false);
        for (var i = 0; i < nodes.length; i++) replaceMathInNode(nodes[i]);
    }

    function applyTextTransform(container, fn) {
        var nodes = collectTextNodes(container, true);
        for (var i = 0; i < nodes.length; i++) {
            var node = nodes[i];
            var value = node.nodeValue;
            if (!value) continue;
            var out = null;
            try {
                out = fn(value, node);
            } catch (e) {
                out = null;
            }
            if (out === null || out === undefined || out === false) continue;
            if (typeof out === 'string') {
                node.nodeValue = out;
            } else if (out.nodeType) {
                var frag = global.document.createDocumentFragment();
                if (out.nodeType === 11) {
                    while (out.firstChild) frag.appendChild(out.firstChild);
                } else {
                    frag.appendChild(out);
                }
                if (node.parentNode) node.parentNode.replaceChild(frag, node);
            }
        }
    }

    function postProcess(safeHtml, opts) {
        var needMath = opts.math !== false && hasKatex();
        var needTransform = typeof opts.transformText === 'function';
        if (!needMath && !needTransform) return safeHtml;
        var tpl = global.document.createElement('template');
        tpl.innerHTML = safeHtml;
        if (needMath) renderMathIn(tpl.content);
        if (needTransform) applyTextTransform(tpl.content, opts.transformText);
        return tpl.innerHTML;
    }

    // ---------------------------------------------------------------- 公开 API

    function normalizeOptions(options) {
        if (!options || typeof options !== 'object') return {};
        return options;
    }

    /** 仅消毒：输入必须是 HTML 字符串（marked 输出等） */
    function sanitizeHtml(dirty) {
        var p = getPurifier();
        var raw = (dirty === null || dirty === undefined) ? '' : String(dirty);
        if (!p) return escapeHtml(raw);
        try {
            return p.sanitize(raw, PURIFY_CONFIG);
        } catch (e) {
            return escapeHtml(raw);
        }
    }

    /** 纯文本：返回 HTML 转义后的字符串（可直接放进 innerHTML，不解释标签） */
    function renderText(text) {
        return escapeHtml(text);
    }

    /** 纯文本 + 换行：仅在没有 markdown 引擎时用于兜底 */
    function textToHtml(text) {
        return escapeHtml(text).replace(/\r\n|\r|\n/g, '<br>');
    }

    /**
     * 唯一的安全 Markdown 渲染入口。
     * @param {string} text 模型/用户文本（不可信）
     * @param {Object} [options] { breaks: boolean, math: boolean, transformText: (text, node) => Node|string|null }
     * @returns {string} 已消毒的 HTML
     */
    function renderMarkdownSafe(text, options) {
        var opts = normalizeOptions(options);
        var source = (text === null || text === undefined) ? '' : String(text);
        if (!source) return '';

        var purifier = getPurifier();
        var markedLib = getMarked();
        if (!purifier || !markedLib) {
            // 依赖缺失：降级纯文本，绝不退回未消毒 HTML
            return textToHtml(source);
        }

        var raw = null;
        try {
            raw = markedLib.parse(source, {
                gfm: true,
                breaks: opts.breaks !== false,
                async: false
            });
        } catch (e) {
            raw = null;
        }
        if (typeof raw !== 'string') return textToHtml(source);

        var safe = '';
        try {
            safe = purifier.sanitize(raw, PURIFY_CONFIG);
        } catch (e) {
            return textToHtml(source);
        }
        return postProcess(safe, opts);
    }

    /** 纯文本写入：优先 textContent，绝不 innerHTML */
    function setText(el, text) {
        if (!el) return;
        el.textContent = (text === null || text === undefined) ? '' : String(text);
    }

    /**
     * 把不可信文本安全写入元素。
     * @param {Element} el 目标元素
     * @param {string} text 不可信文本
     * @param {Object} [options] { asText: true → textContent；否则走 renderMarkdownSafe }
     */
    function renderInto(el, text, options) {
        if (!el) return;
        var opts = normalizeOptions(options);
        if (opts.asText) {
            setText(el, text);
            return;
        }
        el.innerHTML = renderMarkdownSafe(text, opts);
    }

    var api = {
        version: VERSION,
        dompurifyVersion: '3.2.4',
        dompurifySha256: DOMPURIFY_SHA256,
        renderMarkdownSafe: renderMarkdownSafe,
        renderText: renderText,
        sanitizeHtml: sanitizeHtml,
        setText: setText,
        renderInto: renderInto,
        installHooks: function () { return installHooks(global.DOMPurify); },
        isAvailable: function () { return !!(getPurifier() && getMarked()); },
        hasMath: hasKatex,
        _config: {
            forbidTags: FORBID_TAGS.slice(),
            forbidAttr: FORBID_ATTR.slice(),
            safeLink: String(SAFE_LINK_RE),
            unsafeSrc: String(UNSAFE_SRC_RE)
        }
    };

    // 已加载过就不重复覆盖（避免二次加载引入两套 hook 状态）
    if (global.FeClawSafeRender && global.FeClawSafeRender.version) {
        return;
    }
    global.FeClawSafeRender = api;

    // DOMPurify 若已就绪，立即装一次 hook（幂等）
    if (global.DOMPurify) installHooks(global.DOMPurify);

    if (typeof module === 'object' && module && module.exports) {
        module.exports = api;
    }
})(typeof window !== 'undefined' ? window : this);
