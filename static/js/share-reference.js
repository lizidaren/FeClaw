/**
 * Share Reference Token — 分享页文本选中引用功能
 *
 * 用户在分享页选中文本 → 浮动按钮 → 标记为引用 → 复制 reference token
 */
(function () {
    "use strict";

    if (typeof SHARE_HASH === "undefined" || typeof VFS_PATH === "undefined") return;

    var SHARE_HASH_VAL = SHARE_HASH;
    var VFS_PATH_VAL = VFS_PATH;

    // ── data-offset 映射 ──────────────────────────────────────────
    var rawMd = "";
    var textNodeOffsets = [];

    function buildOffsetMap() {
        var article = document.getElementById("c");
        if (!article) return;

        // 尝试从 marked parse 前的 safe_md 获取原始 MD（存在 window 上）
        rawMd = window._RAW_MD || "";

        var walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT, null, false);
        var currentOffset = 0;
        textNodeOffsets = [];

        var node;
        while ((node = walker.nextNode())) {
            var text = node.textContent || "";
            var parent = node.parentElement;
            if (parent && !parent.hasAttribute("data-soff")) {
                parent.setAttribute("data-soff", currentOffset);
                parent.setAttribute("data-eoff", currentOffset + text.length);
                textNodeOffsets.push({
                    node: node,
                    parent: parent,
                    start: currentOffset,
                    end: currentOffset + text.length,
                });
                currentOffset += text.length;
            }
        }
    }

    // 从选中 DOM 范围计算在 rawMd 中的偏移
    function getSelectionOffsets(sel) {
        if (!sel || sel.isCollapsed || !sel.rangeCount) return null;

        var range = sel.getRangeAt(0);
        var startNode = range.startContainer;
        var endNode = range.endContainer;
        var startOffset = range.startOffset;
        var endOffset = range.endOffset;

        // 查找起始偏移
        var startTotal = null;
        var endTotal = null;

        for (var i = 0; i < textNodeOffsets.length; i++) {
            var entry = textNodeOffsets[i];
            if (entry.node === startNode) {
                startTotal = entry.start + startOffset;
            }
            if (entry.node === endNode) {
                endTotal = entry.start + endOffset;
            }
            if (startTotal !== null && endTotal !== null) break;
        }

        // 如果没找到精确匹配，用 data-offset fallback
        if (startTotal === null) {
            var p = startNode.nodeType === 3 ? startNode.parentElement : startNode;
            if (p && p.getAttribute) {
                var soff = parseInt(p.getAttribute("data-soff"));
                if (!isNaN(soff)) startTotal = soff + startOffset;
            }
        }
        if (endTotal === null) {
            var p = endNode.nodeType === 3 ? endNode.parentElement : endNode;
            if (p && p.getAttribute) {
                var soff = parseInt(p.getAttribute("data-soff"));
                if (!isNaN(soff)) endTotal = soff + endOffset;
            }
        }

        if (startTotal === null || endTotal === null) return null;
        if (startTotal > endTotal) {
            var tmp = startTotal;
            startTotal = endTotal;
            endTotal = tmp;
        }
        return { start: startTotal, end: endTotal };
    }

    // ── 浮动按钮 ──────────────────────────────────────────────────
    var floatBtn = null;
    var lastSelection = null;

    function createFloatButton() {
        if (floatBtn) return;
        floatBtn = document.createElement("button");
        floatBtn.textContent = "\uD83D\uDCCC \u6807\u8BB0\u6B64\u6BB5"; // 📌 标记此段
        floatBtn.style.cssText =
            "position:absolute;z-index:9999;display:none;padding:6px 14px;" +
            "background:#1a73e8;color:#fff;border:none;border-radius:6px;" +
            "cursor:pointer;font-size:14px;box-shadow:0 2px 8px rgba(0,0,0,.2);" +
            "white-space:nowrap;transition:opacity .15s;-webkit-tap-highlight-color:transparent;";
        document.body.appendChild(floatBtn);

        floatBtn.addEventListener("click", function (e) {
            e.preventDefault();
            e.stopPropagation();
            handleMarkSelection();
        });

        // 移动端 touch
        floatBtn.addEventListener("touchend", function (e) {
            e.preventDefault();
            e.stopPropagation();
            handleMarkSelection();
        });
    }

    function positionFloatButton() {
        var sel = window.getSelection();
        if (!sel || sel.isCollapsed || !sel.rangeCount) {
            if (floatBtn) floatBtn.style.display = "none";
            return;
        }

        var range = sel.getRangeAt(0);
        var rect = range.getBoundingClientRect();
        if (!rect || (rect.width === 0 && rect.height === 0)) {
            if (floatBtn) floatBtn.style.display = "none";
            return;
        }

        var scrollX = window.scrollX || window.pageXOffset;
        var scrollY = window.scrollY || window.pageYOffset;

        var top = rect.top + scrollY - 40;
        if (top < scrollY + 10) top = rect.bottom + scrollY + 8;

        var left = rect.left + scrollX + rect.width / 2;
        // 转为从左边定位
        floatBtn.style.top = top + "px";
        floatBtn.style.left = left + "px";
        floatBtn.style.transform = "translate(-50%, 0)";
        floatBtn.style.display = "block";
        floatBtn.style.opacity = "1";

        lastSelection = {
            text: sel.toString().trim(),
            offsets: getSelectionOffsets(sel),
        };
    }

    var debounceTimer = null;
    function onSelectionChange() {
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () {
            positionFloatButton();
        }, 200);
    }

    function hideFloatButton(e) {
        if (floatBtn && e && floatBtn.contains(e.target)) return;
        if (floatBtn) {
            floatBtn.style.display = "none";
        }
        lastSelection = null;
    }

    // ── 标记请求 ──────────────────────────────────────────────────
    function showToast(msg, duration) {
        duration = duration || 1500;
        var toast = document.createElement("div");
        toast.textContent = msg;
        toast.style.cssText =
            "position:fixed;bottom:32px;left:50%;transform:translateX(-50%);z-index:99999;" +
            "background:#323232;color:#fff;padding:12px 24px;border-radius:8px;" +
            "font-size:15px;box-shadow:0 4px 12px rgba(0,0,0,.25);" +
            "transition:opacity .3s;opacity:1;max-width:90vw;text-align:center;";
        document.body.appendChild(toast);
        setTimeout(function () {
            toast.style.opacity = "0";
            setTimeout(function () {
                if (toast.parentNode) toast.parentNode.removeChild(toast);
            }, 300);
        }, duration);
    }

    function copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text).then(
                function () { return true; },
                function () { return false; }
            );
        }
        // Fallback: execCommand
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

    function handleMarkSelection() {
        if (floatBtn) floatBtn.style.display = "none";

        if (!lastSelection || !lastSelection.text) {
            showToast("\u26A0\uFE0F \u8BF7\u5148\u9009\u4E2D\u6587\u672C"); // ⚠️ 请先选中文本
            return;
        }

        var selText = lastSelection.text.substring(0, 2000);

        // 获取上下文
        var contextBefore = "";
        var contextAfter = "";
        if (rawMd && lastSelection.offsets) {
            var s = Math.max(0, lastSelection.offsets.start - 200);
            contextBefore = rawMd.substring(s, lastSelection.offsets.start);
            var e = Math.min(rawMd.length, lastSelection.offsets.end + 200);
            contextAfter = rawMd.substring(lastSelection.offsets.end, e);
        }

        var payload = {
            share_hash: SHARE_HASH_VAL,
            vfs_path: VFS_PATH_VAL,
            selected_text: selText,
            context_before: contextBefore.substring(0, 500),
            context_after: contextAfter.substring(0, 500),
        };

        fetch("/api/share/reference", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
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
                    showToast("\uD83D\uDCCC \u5DF2\u6807\u8BB0\uFF1A" + refToken + "\uFF08\u5DF2\u590D\u5236\uFF09");
                    // 📌 已标记：[reference:xxx]（已复制）
                });
            })
            .catch(function (err) {
                showToast("\u274C \u6807\u8BB0\u5931\u8D25\uFF1A" + err.message);
                // ❌ 标记失败：...
            });
    }

    // ── 事件绑定 ──────────────────────────────────────────────────
    function init() {
        buildOffsetMap();
        createFloatButton();

        document.addEventListener("mouseup", onSelectionChange);
        document.addEventListener("touchend", onSelectionChange);
        document.addEventListener("mousedown", hideFloatButton);
        // 触摸开始时不立即隐藏（touchend 先触发再触发 mousedown 补充）
        document.addEventListener("touchstart", function (e) {
            if (floatBtn && !floatBtn.contains(e.target)) {
                setTimeout(function () {
                    if (floatBtn) floatBtn.style.display = "none";
                }, 300);
            }
        });
        // 滚动时隐藏
        window.addEventListener("scroll", function () {
            if (floatBtn) floatBtn.style.display = "none";
        }, { passive: true });

        // 保存 rawMd 到 window（在页面脚本中设置）
        // 尝试从原始 safe_md 变量获取（它已通过 marked.js 渲染成 HTML）
        // 我们只能从 DOM 重建，所以 rawMd 在 share.py 模板中设置
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
