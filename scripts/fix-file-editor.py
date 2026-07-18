import sys

with open('templates/agent_files.html', 'r') as f:
    code = f.read()

# 1. Add dirtyFile tracking variable
code = code.replace(
    "        let currentPath = '';",
    "        let currentPath = '';\n        let dirtyFile = false;        // 编辑器内容是否已修改"
)

# 2. Set dirtyFile on CodeMirror change
code = code.replace(
    "                editor.on('change', function() {\n                    updateMarkdownPreview();\n                });",
    "                editor.on('change', function() {\n                    dirtyFile = true;\n                    updateMarkdownPreview();\n                });"
)

# 3. Add closeModal dirty check
code = code.replace(
    "function closeModal() {\n            document.getElementById('editModal').classList.remove('active');",
    "function closeModal() {\n            if (dirtyFile && !confirm('当前文件有未保存的修改，确定要关闭吗？')) return;\n            dirtyFile = false;\n            document.getElementById('editModal').classList.remove('active');"
)

# 4. Clear dirtyFile on save
code = code.replace(
    "async function saveFile() {\n            if (!editingFile) return;\n            document.getElementById('btnSave').textContent = '保存中…';",
    "async function saveFile() {\n            if (!editingFile) return;\n            dirtyFile = false;\n            document.getElementById('btnSave').textContent = '保存中…';"
)

# 5. Add URL history tracking in navigate()
code = code.replace(
    "        function navigate(path) {\n            currentPath = path;\n            loadFiles();\n        }",
    """        function navigate(path) {
            currentPath = path;
            updateFileHistory();
            loadFiles();
        }
        
        function updateFileHistory() {
            const params = new URLSearchParams(window.location.search);
            params.set('path', currentPath || '');
            const newUrl = window.location.pathname + '?' + params.toString();
            window.history.replaceState({ path: currentPath }, '', newUrl);
        }"""
)

# 6. Add restorePathFromUrl function (after initBackend)
code = code.replace(
    "        function updateBackendBadge() {",
    """        function restorePathFromUrl() {
            const p = new URLSearchParams(window.location.search).get('path');
            if (p !== null && p !== '') {
                currentPath = p;
                loadFiles();
            }
        }

        function updateBackendBadge() {"""
)

# 7. Auto-restore from URL after COS init
code = code.replace(
    "                updateBackendBadge();\n                loadFiles();\n            } catch (e) {",
    "                updateBackendBadge();\n                restorePathFromUrl();\n                loadFiles();\n            } catch (e) {"
)

# 8. Also for local fallback in catch
code = code.replace(
    "                updateBackendBadge();\n                loadFiles();\n            }\n        }\n\n        // 从 URL",
    "                updateBackendBadge();\n                restorePathFromUrl();\n                loadFiles();\n            }\n        }\n\n        // 从 URL"
)

with open('templates/agent_files.html', 'w') as f:
    f.write(code)
print("✅ 编辑器关闭确认 + URL 路径持久化")
