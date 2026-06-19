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
            btn.addEventListener("click", function (e) {
                e.preventDefault();
                e.stopPropagation();
                handleAction(this.getAttribute("data-action"));
            });
            // Mobile
            btn.addEventListener("touchend", function (e) {
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
        return true;
    }

    function hideToolbar() {
        if (toolbar) toolbar.style.display = "none";
    }

    // ── 操作 ──────────────────────────────────────────────────────
    function handleAction(action) {
        hideToolbar();
        var text = getSelectedText();
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
                    showToast("\uD83D\uDCCC \u5F15\u7528\u5DF2\u590D\u5236\uFF1A" + refToken);
                });
            })
            .catch(function (err) {
                showToast("\u274C \u5F15\u7528\u5931\u8D25\uFF1A" + err.message);
            });
    }

    // ── Toast ────────────────────────────────────────────────────
    function showToast(msg) {
        if (toastEl && toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
        toastEl = document.createElement("div");
        toastEl.textContent = msg;
        toastEl.style.cssText =
            "position:fixed;bottom:32px;left:50%;transform:translateX(-50%);z-index:99999;" +
            "background:#323232;color:#fff;padding:12px 24px;border-radius:8px;" +
            "font-size:15px;box-shadow:0 4px 12px rgba(0,0,0,.25);" +
            "transition:opacity .3s;opacity:1;max-width:90vw;text-align:center;";
        document.body.appendChild(toastEl);
        setTimeout(function () {
            toastEl.style.opacity = "0";
            setTimeout(function () {
                if (toastEl && toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
            }, 300);
        }, 2000);
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
