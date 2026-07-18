import sys

with open('templates/agent_files.html', 'r') as f:
    code = f.read()

# 1. Remove duplicate dirtyFile declaration (there can't be two)
code = code.replace(
    "        let dirtyFile = false;        // 编辑器内容是否已修改\n        let dirtyFile = false;        // 编辑器内容是否已修改",
    "        let dirtyFile = false;        // 编辑器内容是否已修改"
)

# 2. Replace the confirm() in closeModal with a custom modal call
# Also remove the second dirtyFile = false since we'll handle in saveFile
old_close = """function closeModal() {
            if (dirtyFile && !confirm('当前文件有未保存的修改，确定要关闭吗？')) return;
            dirtyFile = false;"""

new_close = """function closeModal() {
            if (dirtyFile) {
                showUnsavedDialog();
                return;
            }"""

code = code.replace(old_close, new_close, 1)

# 3. Add CSS for custom dialog
old_css_end = "</style>"
new_css = """/* 自定义确认对话框 */
        .unsaved-overlay {
            display:none; position:fixed; inset:0; background:rgba(0,0,0,0.3);
            z-index:10000; align-items:center; justify-content:center;
        }
        .unsaved-overlay.active { display:flex; }
        .unsaved-dialog {
            background:white; border-radius:12px; padding:28px 32px;
            max-width:420px; width:90%; box-shadow:0 8px 32px rgba(0,0,0,0.15);
            text-align:center;
        }
        .unsaved-dialog h3 { margin:0 0 8px; color:#1a1a2e; font-size:18px; }
        .unsaved-dialog p { margin:0 0 24px; color:#666; font-size:14px; line-height:1.5; }
        .unsaved-actions { display:flex; gap:12px; justify-content:center; }
        .unsaved-actions button {
            padding:10px 24px; border-radius:8px; border:none; font-size:14px;
            cursor:pointer; font-weight:600; transition:all 0.2s;
        }
        .unsaved-actions .btn-discard { background:#f0f0f0; color:#666; }
        .unsaved-actions .btn-discard:hover { background:#e0e0e0; }
        .unsaved-actions .btn-cancel { background:#f0f0f0; color:#666; }
        .unsaved-actions .btn-cancel:hover { background:#e0e0e0; }
        .unsaved-actions .btn-save-close {
            background:linear-gradient(135deg,#667eea,#764ba2); color:white;
        }
        .unsaved-actions .btn-save-close:hover { opacity:0.9; }
        </style>"""

code = code.replace(old_css_end, new_css, 1)

# 4. Add HTML for custom dialog (before </body>)
old_body_end = "</body>"
new_html = """    <!-- 未保存确认对话框 -->
    <div class="unsaved-overlay" id="unsavedOverlay">
        <div class="unsaved-dialog">
            <h3>📝 未保存的修改</h3>
            <p>当前文件有未保存的更改<br>要保存后再关闭吗？</p>
            <div class="unsaved-actions">
                <button class="btn-discard" onclick="discardAndClose()">不保存</button>
                <button class="btn-save-close" onclick="saveAndClose()">保存并关闭</button>
            </div>
        </div>
    </div>
</body>"""

code = code.replace(old_body_end, new_html, 1)

# 5. Add JS functions for unsaved dialog + restore dirtyFile reset
old_after_close = """}
        
        // 未保存确认 —— 由 closeModal 触发"""
        
# Check if this already exists
if "discardAndClose" not in code:
    code = code.replace(
        "        function updateFileHistory() {",
        """        function discardAndClose() {
            dirtyFile = false;
            document.getElementById('unsavedOverlay').classList.remove('active');
            editingFile = null;
            closeModalDirect();
        }
        
        function saveAndClose() {
            document.getElementById('unsavedOverlay').classList.remove('active');
            saveFile().then(() => {
                dirtyFile = false;
                editingFile = null;
                closeModalDirect();
            }).catch(() => {
                // 保存失败 - 保持打开状态
            });
        }
        
        function showUnsavedDialog() {
            document.getElementById('unsavedOverlay').classList.add('active');
        }
        
        function closeModalDirect() {
            document.getElementById('editModal').classList.remove('active');
            if (editor) { editor.getTextArea().style.display = 'block'; editor.toTextArea(); editor = null; }
            const body = document.querySelector('.editor-body');
            if (body) body.style.display = '';
            document.getElementById('editorContainer').style.display = 'block';
            document.getElementById('configEditor').style.display = 'none';
            document.getElementById('configDesc').style.display = 'none';
            document.getElementById('configValueEdit').style.display = 'block';
            document.getElementById('imagePreview').style.display = 'none';
            document.getElementById('markdownPreview').classList.remove('active');
            document.getElementById('mediaPreview').classList.remove('active');
            document.getElementById('mediaPreview').innerHTML = '';
            document.getElementById('unsupportedPreview').classList.remove('active');
            document.getElementById('modeToggleBtn').style.display = 'none';
            document.getElementById('modalActions').style.display = 'flex';
            document.getElementById('btnSave').style.display = '';
            document.getElementById('editorPaneHeader').style.display = '';
            currentMode = 'edit';
            currentFileType = 'text';
        }

        function updateFileHistory() {"""
    )

with open('templates/agent_files.html', 'w') as f:
    f.write(code)
print("✅ 自定义未保存对话框 + 编辑器修复")
