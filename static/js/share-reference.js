/**
 * Share Reference Token — 分享页文本选中工具栏
 *
 * 完全接管文本选中菜单，显示：复制 / 引用 / 全选
 * 拦截手机长按系统菜单（contextmenu），自定义弹出工具栏
 */
(function () {
    "use strict";
    if (typeof SHARE_HASH === "undefined" || typeof VFS_PATH === "undefined") return;

    var SHARE_HASH_VAL = SHARE_HASH;
    var VFS_PATH_VAL = VFS_PATH;
    var rawMd = window._RAW_MD || "";
    var toolbar = null;
    var toastEl = null;
    var _savedSelection = "";   // 在工具栏显示时保存选中的文本，避免 iOS 点击后丢失选择

    // ── rawMd 文本查找 ──────────────────────────────────────────────
    function findTextInRawMd(selectedText) {
        if (!rawMd || !selectedText) return null;
        var idx = rawMd.lastIndexOf(selectedText);
        if (idx === -1) return null;
        return { start: idx, end: idx + selectedText.length };
    }

    // ── 工具栏 DOM ─────────────────────────────────────────────────
    function createToolbar() {
        if (toolbar) return;
        toolbar = document.createElement("div");
        toolbar.style.cssText =
            "position:fixed;z-index:9999;display:none;" +
            "background:#2d2d2d;color:#fff;border-radius:10px;" +
            "padding:4px;box-shadow:0 4px 16px rgba(0,0,0,.3);" +
            "font-size:14px;white-space:nowrap;user-select:none;" +
            "-webkit-user-select:none;";

        var btnStyle =
            "background:transparent;color:#fff;border:none;" +
            "padding:8px 14px;font-size:14px;cursor:pointer;" +
            "border-radius:6px;white-space:nowrap;";

        toolbar.innerHTML =
            '<button style="' + btnStyle + '" data-action="copy">\uD83D\uDCCB \u590D\u5236</button>' +
            '<span style="color:#555;padding:0 2px;">|</span>' +
            '<button style="' + btnStyle + '" data-action="reference">\uD83D\uDCCC \u5F15\u7528</button>' +
            '<span style="color:#555;padding:0 2px;">|</span>' +
            '<button style="' + btnStyle + '" data-action="selectall">\uD83D\uDD0D \u5168\u9009</button>';

        // Hover feedback
        toolbar.querySelectorAll("button").forEach(function (btn) {
            btn.addEventListener("mouseenter", function () {
                this.style.background = "rgba(255,255,255,0.1)";
            });
            btn.addEventListener("mouseleave", function () {
                this.style.background = "transparent";
            });
            // Mobile: 只监听 touchend，防止 click 重复触发
            // touchstart 触发后设置标记阻止后续 click
            btn._touchFired = false;
            btn.addEventListener("touchstart", function () {
                this._touchFired = true;
            });
            btn.addEventListener("touchend", function (e) {
                e.preventDefault();
                e.stopPropagation();
                handleAction(this.getAttribute("data-action"));
            });
            // 鼠标设备：click 正常触发；如果已走过 touchend，跳过 click
            var _origClick = btn.addEventListener;
            btn.addEventListener("click", function (e) {
                if (this._touchFired) { this._touchFired = false; return; }
                e.preventDefault();
                e.stopPropagation();
                handleAction(this.getAttribute("data-action"));
            });
        });

        document.body.appendChild(toolbar);
    }

    function getSelectedText() {
        var sel = window.getSelection();
        return sel && !sel.isCollapsed ? sel.toString().trim() : "";
    }

    function positionToolbar() {
        var sel = window.getSelection();
        if (!sel || sel.isCollapsed || !sel.rangeCount) {
            hideToolbar();
            return false;
        }

        var range = sel.getRangeAt(0);
        var rect = range.getBoundingClientRect();
        if (!rect || (rect.width === 0 && rect.height === 0)) {
            hideToolbar();
            return false;
        }

        var w = toolbar.offsetWidth || 280;
        var h = toolbar.offsetHeight || 40;
        var left = rect.left + rect.width / 2 - w / 2;
        var top = rect.top - h - 8;

        // 上边空间不够则显示在下方
        if (top < 8) top = rect.bottom + 8;

        // 不超出屏幕
        if (left < 8) left = 8;
        if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;

        toolbar.style.left = left + "px";
        toolbar.style.top = top + "px";
        toolbar.style.display = "block";
        _savedSelection = getSelectedText();
        return true;
    }

    function hideToolbar() {
        if (toolbar) toolbar.style.display = "none";
    }

    // ── 操作 ──────────────────────────────────────────────────────
    function handleAction(action) {
        hideToolbar();
        var text = getSelectedText() || _savedSelection;
        _savedSelection = "";
        if (!text) return;

        if (action === "copy") {
            copyToClipboard(text).then(function () {
                showToast("\u2705 \u5DF2\u590D\u5236");
            });
        } else if (action === "selectall") {
            var article = document.getElementById("c");
            if (article) {
                var range = document.createRange();
                range.selectNodeContents(article);
                var sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                setTimeout(positionToolbar, 100);
            }
        } else if (action === "reference") {
            handleReference(text);
        }
    }

    function handleReference(selText) {
        selText = selText.substring(0, 2000);

        var contextBefore = "";
        var contextAfter = "";
        if (rawMd && selText) {
            var offsets = findTextInRawMd(selText);
            if (offsets) {
                var s = Math.max(0, offsets.start - 200);
                contextBefore = rawMd.substring(s, offsets.start);
                var e = Math.min(rawMd.length, offsets.end + 200);
                contextAfter = rawMd.substring(offsets.end, e);
            }
        }

        fetch("/api/share/reference", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                share_hash: SHARE_HASH_VAL,
                vfs_path: VFS_PATH_VAL,
                selected_text: selText,
                context_before: contextBefore.substring(0, 500),
                context_after: contextAfter.substring(0, 500),
            }),
        })
            .then(function (resp) {
                if (!resp.ok) {
                    return resp.json().then(function (data) {
                        throw new Error(data.detail || "Request failed");
                    });
                }
                return resp.json();
            })
            .then(function (data) {
                var refToken = "[reference:" + data.ref_hash + "]";
                return copyToClipboard(refToken).then(function () {
                    showReferenceModal(selText, contextBefore, contextAfter, refToken);
                });
            })
            .catch(function (err) {
                showErrorToast("\u274C \u5F15\u7528\u5931\u8D25\uFF1A" + err.message);
            });
    }

    // ── 引用成功弹窗 ─────────────────────────────────────────────
    function showReferenceModal(selText, ctxBefore, ctxAfter, refToken) {
        var overlay = document.createElement("div");
        overlay.className = "feclaw-ref-overlay";
        overlay.style.cssText =
            "position:fixed;top:0;left:0;width:100%;height:100%;z-index:99998;" +
            "background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;" +
            "animation:fadeIn .2s;";

        // Build selected text display (rendered as markdown, dark theme)
        var displayMd = "";
        if (ctxBefore) {
            displayMd += ctxBefore.slice(-120);
        }
        displayMd += "**" + selText + "**";
        if (ctxAfter) {
            displayMd += ctxAfter.slice(0, 120);
        }
        var renderedHtml = marked.parse(displayMd);

        // Strip github-markdown-body white background: wrap in a div that resets it
        renderedHtml = '<div class="feclaw-ref-markdown" style="color:#d4d4d4;font-size:14px;line-height:1.8;">' +
            renderedHtml + '</div>';

        var modal = document.createElement("div");
        modal.style.cssText =
            "background:#2a2a2a;color:#e0e0e0;border-radius:16px;padding:0;max-width:520px;" +
            "width:90vw;box-shadow:0 12px 48px rgba(0,0,0,0.5);overflow:hidden;";

        modal.innerHTML =
            // Header
            '<div style="background:#3a3a3a;padding:16px 20px;font-size:17px;font-weight:600;' +
            'display:flex;align-items:center;gap:8px;">' +
            '\uD83D\uDCCC \u5F15\u7528\u590D\u5236\u6210\u529F\uFF01</div>' +
            // Selected text (MD rendered, dark theme)
            '<div style="padding:16px 20px 8px;">' +
            '<div style="font-size:12px;color:#888;margin-bottom:6px;">\u9009\u4E2D\u5185\u5BB9</div>' +
            '<div style="max-height:180px;overflow-y:auto;word-break:break-word;' +
            'background:#1e1e1e;border:1px solid #3a3a3a;border-radius:8px;padding:12px;">' +
            renderedHtml +
            '</div></div>' +
            // Reference token
            '<div style="padding:0 20px 8px;">' +
            '<div style="font-size:12px;color:#888;margin-bottom:6px;">\u5F15\u7528\u6807\u8BB0\uFF08\u5DF2\u81EA\u52A8\u590D\u5236\uFF09</div>' +
            '<div style="background:#1e1e1e;border:1px solid #555;border-radius:8px;padding:10px 14px;' +
            'font-family:monospace;font-size:14px;color:#7ecfff;word-break:break-all;cursor:pointer;" ' +
            'id="feclaw-ref-token" onclick="navigator.clipboard.writeText(this.textContent).catch(()=>{})">' +
            refToken +
            '</div></div>' +
            // Instruction
            '<div style="padding:0 20px 16px;">' +
            '<div style="font-size:13px;color:#aaa;line-height:1.6;">' +
            '\u53D1\u9001\u8FD9\u4E2A\u5F15\u7528\u6807\u8BB0\u7ED9Agent\uFF0CAgent\u53EF\u4EE5\u770B\u5230\u4F60\u5F15\u7528\u7684\u5185\u5BB9\u3002' +
            '\u4F8B\u5982\uFF1A<span style="color:#7ecfff;font-family:monospace;">' + refToken + '</span>\u80FD\u89E3\u91CA\u4E00\u4E0B\u8FD9\u4E2A\u5417' +
            '</div></div>' +
            // Close button
            '<div style="padding:0 20px 16px;text-align:right;">' +
            '<button style="background:#5b7cfa;color:#fff;border:none;border-radius:8px;' +
            'padding:10px 28px;font-size:15px;cursor:pointer;" ' +
            'onclick="this.closest(\'.feclaw-ref-overlay\').remove()">\u77E5\u9053\u4E86</button>' +
            '</div>';

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        // Click outside to close
        overlay.addEventListener("click", function (e) {
            if (e.target === overlay) overlay.remove();
        });
    }

    function escapeHtml(str) {
        var div = document.createElement("div");
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    // ── Toast (only for errors now) ─────────────────────────────
    function showErrorToast(msg) {
        if (toastEl && toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
        toastEl = document.createElement("div");
        toastEl.textContent = msg;
        toastEl.style.cssText =
            "position:fixed;bottom:32px;left:50%;transform:translateX(-50%);z-index:99999;" +
            "background:#c0392b;color:#fff;padding:12px 24px;border-radius:8px;" +
            "font-size:15px;box-shadow:0 4px 12px rgba(0,0,0,.25);" +
            "opacity:1;max-width:90vw;text-align:center;";
        document.body.appendChild(toastEl);
        setTimeout(function () {
            toastEl.style.opacity = "0";
            setTimeout(function () {
                if (toastEl && toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
            }, 300);
        }, 2500);
    }

    // ── 剪贴板 ────────────────────────────────────────────────────
    function copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text).then(
                function () { return true; },
                function () { return false; }
            );
        }
        try {
            var ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.left = "-9999px";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
            return Promise.resolve(true);
        } catch (e) {
            return Promise.resolve(false);
        }
    }

    // ── 选中事件 ──────────────────────────────────────────────────
    var debounceTimer = null;
    function onSelectionChange() {
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () {
            if (!getSelectedText()) {
                hideToolbar();
                return;
            }
            positionToolbar();
        }, 150);
    }

    // ── 初始化 ────────────────────────────────────────────────────
    function init() {
        createToolbar();

        // 拦截系统菜单（手机长按 / 桌面右键）
        document.addEventListener("contextmenu", function (e) {
            if (getSelectedText()) {
                e.preventDefault();
                // 弹出我们的工具栏
                positionToolbar();
            }
        });

        // 选中后显示工具栏
        document.addEventListener("mouseup", onSelectionChange);
        document.addEventListener("touchend", onSelectionChange);
        document.addEventListener("keyup", onSelectionChange); // Ctrl+A

        // 点其他地方隐藏
        document.addEventListener("mousedown", function (e) {
            if (toolbar && !toolbar.contains(e.target)) {
                hideToolbar();
            }
        });
        document.addEventListener("touchstart", function (e) {
            if (toolbar && !toolbar.contains(e.target)) {
                setTimeout(hideToolbar, 200);
            }
        });

        // 滚动隐藏
        window.addEventListener("scroll", hideToolbar, { passive: true });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
