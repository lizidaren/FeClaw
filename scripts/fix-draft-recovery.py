import sys

with open('templates/agent_files.html', 'r') as f:
    c = f.read()

# 1. Change the silent draft recovery in openMarkdownFile to use a custom dialog
old_md_recovery = """                // 恢复未保存的草稿（通过 localStorage 自动保存的）
                try {
                    var saved = localStorage.getItem(LS_PREFIX + 'unsaved_' + path);
                    if (saved && saved !== content && saved.length > 0) {
                        var dp = localStorage.getItem(LS_PREFIX + 'draft_path');
                        if (dp === path) { content = saved; dirtyFile = true; }
                    }
                } catch(e) {}"""

new_md_recovery = """                // 检查草稿，延迟弹窗询问（由 openFile 统一处理）
                // （草稿恢复逻辑在 draftConfirm 流程中处理）"""

c = c.replace(old_md_recovery, new_md_recovery, 1)

# 2. Same for openTextFile
old_txt_recovery = """                // 恢复未保存的草稿
                try {
                    var saved = localStorage.getItem(LS_PREFIX + 'unsaved_' + path);
                    if (saved && saved !== content && saved.length > 0) {
                        var dp = localStorage.getItem(LS_PREFIX + 'draft_path');
                        if (dp === path) { content = saved; dirtyFile = true; }
                    }
                } catch(e) {}"""

new_txt_recovery = """                // 草稿恢复逻辑统一在 openFile → draftConfirm 中处理"""

c = c.replace(old_txt_recovery, new_txt_recovery, 1)

# 3. Add draft overlay HTML after the unsaved overlay
old_body_end = """    <div class="unsaved-overlay" id="unsavedOverlay">"""
new_body_end = """    <!-- 草稿恢复确认对话框 -->
    <div class="unsaved-overlay" id="draftOverlay">
        <div class="unsaved-dialog">
            <div style="text-align:center;margin-bottom:16px;">
                <div style="width:48px;height:48px;border-radius:50%;background:#e8f5e9;display:inline-flex;align-items:center;justify-content:center;">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#2e7d32" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>
                        <line x1="16" y1="17" x2="8" y2="17"/>
                    </svg>
                </div>
            </div>
            <h3 style="margin:0 0 4px;color:#1a1a2e;font-size:16px;font-weight:600;">检测到未保存的编辑</h3>
            <p style="margin:0 0 20px;color:#888;font-size:13px;" id="draftOverlayMsg">上次编辑的内容尚未保存，是否继续编辑？</p>
            <div class="unsaved-actions">
                <button class="btn-save-close" onclick="restoreDraft()" style="order:1">继续编辑</button>
                <button class="btn-discard" onclick="discardDraft()" style="order:2">放弃草稿</button>
            </div>
        </div>
    </div>
    <div class="unsaved-overlay" id="unsavedOverlay">"""

c = c.replace(old_body_end, new_body_end, 1)

# 4. Add draft recovery functions + auto-open on load
# Find where to insert - right after discardAndClose
old_insert_anchor = """        function saveAndClose() {"""
new_insert = """        var pendingDraftPath = null;   // 等待用户确认的草稿路径

        function restoreDraft() {
            document.getElementById('draftOverlay').classList.remove('active');
            if (!pendingDraftPath) return;
            try {
                var saved = localStorage.getItem(LS_PREFIX + 'unsaved_' + pendingDraftPath);
                if (saved) {
                    // 重新打开文件，使用草稿内容
                    dirtyFile = true;
                    openFile(pendingDraftPath);
                }
            } catch(e) {}
            pendingDraftPath = null;
        }

        function discardDraft() {
            document.getElementById('draftOverlay').classList.remove('active');
            if (pendingDraftPath) {
                try {
                    localStorage.removeItem(LS_PREFIX + 'unsaved_' + pendingDraftPath);
                    localStorage.removeItem(LS_PREFIX + 'draft_path');
                } catch(e) {}
            }
            pendingDraftPath = null;
        }

        function checkUnsavedDraft(path, fileContent) {
            // 检查是否有草稿，有则弹窗询问
            try {
                var saved = localStorage.getItem(LS_PREFIX + 'unsaved_' + path);
                if (saved && saved !== fileContent && saved.length > 0) {
                    var dp = localStorage.getItem(LS_PREFIX + 'draft_path');
                    if (dp === path) {
                        pendingDraftPath = path;
                        document.getElementById('draftOverlayMsg').textContent =
                            '文件「' + path + '」有未保存的编辑，是否继续编辑？';
                        document.getElementById('draftOverlay').classList.add('active');
                        return true;
                    }
                }
            } catch(e) {}
            return false;
        }

        function autoRestoreOnLoad() {
            // 页面加载后检测是否有草稿，自动恢复
            try {
                var dp = localStorage.getItem(LS_PREFIX + 'draft_path');
                if (!dp) return;
                var saved = localStorage.getItem(LS_PREFIX + 'unsaved_' + dp);
                if (!saved || saved.length === 0) return;
                // 导航到文件所在目录
                var dir = '';
                var slashIdx = dp.lastIndexOf('/');
                if (slashIdx >= 0) {
                    dir = dp.substring(0, slashIdx);
                }
                // 等待文件列表加载完成后再打开文件
                var checkReady = function() {
                    if (typeof navigate === 'function' && typeof openFile === 'function') {
                        if (dir !== currentPath) {
                            navigate(dir);
                        }
                        // 延迟打开文件（等列表渲染）
                        setTimeout(function() {
                            pendingDraftPath = dp;
                            fetchVFSFileRaw(dp).then(function(content) {
                                document.getElementById('draftOverlayMsg').textContent =
                                    '文件「' + dp + '」有未保存的编辑，是否继续编辑？';
                                document.getElementById('draftOverlay').classList.add('active');
                            }).catch(function() {
                                // 文件可能已被删除，直接恢复草稿
                                dirtyFile = true;
                                openFile(dp);
                            });
                        }, 300);
                    } else {
                        setTimeout(checkReady, 200);
                    }
                };
                setTimeout(checkReady, 500);
            } catch(e) {}
        }

        function saveAndClose() {"""

c = c.replace(old_insert_anchor, new_insert, 1)

# 5. Call autoRestoreOnLoad after initBackend
# Find the final loadFiles() call in the catch block
old_init_end = """                updateBackendBadge();
                restorePathFromUrl();
            }
        }"""

new_init_end = """                updateBackendBadge();
                restorePathFromUrl();
            }
            // 初始化完成后检查草稿
            setTimeout(autoRestoreOnLoad, 100);
        }"""

c = c.replace(old_init_end, new_init_end, 1)

# 6. Add fetchVFSFileRaw helper (for getting file content without opening editor)
old_fetch_helpers = """        // --- 后端无关的文件操作（根据 STORAGE_BACKEND 分派） ---"""

new_fetch_helpers = """        // --- 草稿恢复：获取文件原始内容 ---
        async function fetchVFSFileRaw(path) {
            // 获取文件原始内容（不打开编辑器）
            try {
                var url = STORAGE_BACKEND === 'local'
                    ? '/api/file/raw?path=' + encodeURIComponent(path) + (agentHash ? '&agent_hash=' + encodeURIComponent(agentHash) : '')
                    : '/api/file?path=' + encodeURIComponent(path) + (agentHash ? '&agent_hash=' + encodeURIComponent(agentHash) : '');
                var res = await fetch(url);
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return await res.text();
            } catch(e) {
                throw e;
            }
        }

        // --- 后端无关的文件操作（根据 STORAGE_BACKEND 分派） ---"""

c = c.replace(old_fetch_helpers, new_fetch_helpers, 1)

# 7. In openFile, add draft check BEFORE the existing file open logic
old_openfile_start = """async function openFile(path) {
            editingFile = path;"""

new_openfile_start = """async function openFile(path) {
            // 如果 pendingDraftPath 匹配，说明是草稿恢复，静默继续
            var isDraftRestore = false;
            if (pendingDraftPath === path) {
                isDraftRestore = true;
                pendingDraftPath = null;
            }
            editingFile = path;"""

c = c.replace(old_openfile_start, new_openfile_start, 1)

# In getFilePreviewUrl, handle draft restore: use the raw content instead of fetching
old_preview = """        async function getFilePreviewUrl(path) {
            // 返回可直接用于 <img>/<video>/fetch 的 URL
            if (STORAGE_BACKEND === 'local') {
                const hp = agentHash ? '&agent_hash=' + encodeURIComponent(agentHash) : '';
                return '/api/file/raw?path=' + encodeURIComponent(path) + hp;
            }"""

new_preview = """        async function getFilePreviewUrl(path, useSavedContent) {
            // 返回可直接用于 <img>/<video>/fetch 的 URL
            // useSavedContent=true 时检查 localStorage 草稿
            if (useSavedContent) {
                try {
                    var saved = localStorage.getItem(LS_PREFIX + 'unsaved_' + path);
                    if (saved && saved.length > 0) {
                        var dp = localStorage.getItem(LS_PREFIX + 'draft_path');
                        if (dp === path) {
                            return 'data:text/plain,' + encodeURIComponent(saved);
                        }
                    }
                } catch(e) {}
            }
            if (STORAGE_BACKEND === 'local') {
                const hp = agentHash ? '&agent_hash=' + encodeURIComponent(agentHash) : '';
                return '/api/file/raw?path=' + encodeURIComponent(path) + hp;
            }"""

c = c.replace(old_preview, new_preview, 1)

with open('templates/agent_files.html', 'w') as f:
    f.write(c)
print("✅ 双保险草稿恢复")
