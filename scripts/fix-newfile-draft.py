import sys

with open('templates/agent_files.html', 'r') as f:
    c = f.read()

# 1. Fix draft restore: show edit mode (not preview) for markdown
old_draft_cb = """                        window.__draftRestoreCallback = function() {
                            var isMarkdown = (category === 'markdown');
                            if (isMarkdown) {
                                initEditor(saved, 'markdown');
                                showMarkdownPreview(saved);
                            } else {"""

new_draft_cb = """                        window.__draftRestoreCallback = function() {
                            var isMarkdown = (category === 'markdown');
                            if (isMarkdown) {
                                // 切换为编辑模式，而非预览
                                document.getElementById('editorContainer').style.display = 'block';
                                initEditor(saved, 'markdown');
                                var toggleBtn = document.getElementById('modeToggleBtn');
                                if (toggleBtn) { toggleBtn.style.display = ''; toggleBtn.textContent = '预览'; toggleBtn.classList.remove('active'); }
                                currentMode = 'edit';
                            } else {"""

c = c.replace(old_draft_cb, new_draft_cb, 1)

# 2. Add new file dialog HTML before draftOverlay
old_draft_html = """    <!-- 草稿恢复确认对话框 -->
    <div class="unsaved-overlay" id="draftOverlay">"""
new_file_dialog = """    <!-- 新建文件/目录对话框 -->
    <div class="unsaved-overlay" id="newFileOverlay">
        <div class="unsaved-dialog" style="text-align:left;max-width:380px;">
            <h3 style="margin:0 0 12px;color:#1a1a2e;font-size:16px;font-weight:600;" id="newFileDialogTitle">新建文件</h3>
            <p style="margin:0 0 4px;color:#888;font-size:13px;">文件名</p>
            <input type="text" id="newFileNameInput" placeholder="example.md" style="width:100%;padding:10px 14px;border:1px solid #ddd;border-radius:8px;font-size:14px;outline:none;box-sizing:border-box;margin-bottom:16px;" autofocus>
            <div style="display:flex;gap:12px;justify-content:flex-end;">
                <button class="btn-discard" onclick="document.getElementById('newFileOverlay').classList.remove('active')">取消</button>
                <button class="btn-save-close" id="btnNewFileConfirm" onclick="newFileConfirm()">创建</button>
            </div>
        </div>
    </div>
    <!-- 草稿恢复确认对话框 -->
    <div class="unsaved-overlay" id="draftOverlay">"""

c = c.replace(old_draft_html, new_file_dialog, 1)

# 3. Replace newFile() - use custom dialog instead of prompt
old_newfile = """        // --- 新建文件 ---

        function newFile() {
            const name = prompt('请输入文件名:');
            if (!name) return;

            if (isReadonlyPath()) {
                alert('只读目录不允许新建文件');
                return;
            }

            const path = currentPath ? currentPath + '/' + name : name;
            editingFile = path;
            document.getElementById('editFileName').textContent = '新建: ' + name;
            document.getElementById('editModal').classList.add('active');
            if (editor) { editor.getTextArea().style.display = 'block'; editor.toTextArea(); editor = null; }
            document.getElementById('configEditor').style.display = 'none';
            document.getElementById('editorContainer').style.display = 'block';
            document.getElementById('modeToggleBtn').style.display = 'none';
            document.getElementById('modalActions').style.display = 'flex';
            document.getElementById('btnSave').style.display = '';
            document.getElementById('editorPaneHeader').style.display = '';
            hideAllPanes();
            const mode = detectCodeMirrorMode(path);
            initEditor('', mode);
            currentMode = 'edit';
            currentFileType = 'text';
        }"""

new_newfile = """        // --- 新建文件 ---

        function newFile() {
            if (isReadonlyPath()) {
                alert('只读目录不允许新建文件');
                return;
            }
            document.getElementById('newFileDialogTitle').textContent = '新建文件';
            document.getElementById('btnNewFileConfirm').textContent = '创建';
            document.getElementById('newFileNameInput').value = '';
            document.getElementById('newFileNameInput').placeholder = 'example.md';
            document.getElementById('newFileNameInput').dataset.mode = 'file';
            document.getElementById('newFileOverlay').classList.add('active');
            setTimeout(function() { document.getElementById('newFileNameInput').focus(); }, 100);
        }

        function newFolder() {
            if (isReadonlyPath()) {
                alert('只读目录不允许新建目录');
                return;
            }
            document.getElementById('newFileDialogTitle').textContent = '新建目录';
            document.getElementById('btnNewFileConfirm').textContent = '创建';
            document.getElementById('newFileNameInput').value = '';
            document.getElementById('newFileNameInput').placeholder = 'my-folder';
            document.getElementById('newFileNameInput').dataset.mode = 'dir';
            document.getElementById('newFileOverlay').classList.add('active');
            setTimeout(function() { document.getElementById('newFileNameInput').focus(); }, 100);
        }

        function newFileConfirm() {
            var name = document.getElementById('newFileNameInput').value.trim();
            if (!name) return;
            var mode = document.getElementById('newFileNameInput').dataset.mode;
            document.getElementById('newFileOverlay').classList.remove('active');
            if (mode === 'dir') {
                // 创建目录：在目录内放一个空 .directory 标记文件
                var dirPath = currentPath ? currentPath + '/' + name : name;
                var markerPath = dirPath + '/.directory';
                saveContentFile(markerPath, '').then(function() {
                    loadFiles();
                }).catch(function(e) {
                    alert('创建目录失败: ' + e.message);
                });
                return;
            }
            // 创建文件
            if (isReadonlyPath()) return;
            const path = currentPath ? currentPath + '/' + name : name;
            editingFile = path;
            document.getElementById('editFileName').textContent = '新建: ' + name;
            document.getElementById('editModal').classList.add('active');
            if (editor) { editor.getTextArea().style.display = 'block'; editor.toTextArea(); editor = null; }
            document.getElementById('configEditor').style.display = 'none';
            document.getElementById('editorContainer').style.display = 'block';
            document.getElementById('modeToggleBtn').style.display = 'none';
            document.getElementById('modalActions').style.display = 'flex';
            document.getElementById('btnSave').style.display = '';
            document.getElementById('editorPaneHeader').style.display = '';
            hideAllPanes();
            const cmode = detectCodeMirrorMode(path);
            initEditor('', cmode);
            currentMode = 'edit';
            currentFileType = 'text';
        }"""

c = c.replace(old_newfile, new_newfile, 1)

# 4. Add new folder button next to "新建" button
old_buttons = """        <button class="primary" onclick="newFile()" id="btnNewFile">📝 新建</button>"""
new_buttons = """        <button class="primary" onclick="newFile()" id="btnNewFile">📝 新建文件</button>
        <button class="primary" onclick="newFolder()" id="btnNewDir" style="margin-left:6px">📁 新建目录</button>"""
c = c.replace(old_buttons, new_buttons, 1)

# 5. Fix the newFile overlay to close on Enter
old_overlay_js = """                <button class="btn-save-close" id="btnNewFileConfirm" onclick="newFileConfirm()">创建</button>
            </div>
        </div>
    </div>"""
new_overlay_js = """                <button class="btn-save-close" id="btnNewFileConfirm" onclick="newFileConfirm()">创建</button>
            </div>
        </div>
    </div>
    <script>
        document.getElementById('newFileNameInput').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') newFileConfirm();
        });
    </script>"""
c = c.replace(old_overlay_js, new_overlay_js, 1)

with open('templates/agent_files.html', 'w') as f:
    f.write(c)
print("✅ 草稿编辑模式 + 自定义新建对话框 + 新建目录")
