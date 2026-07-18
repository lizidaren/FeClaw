import sys

with open('templates/agent_files.html', 'r') as f:
    code = f.read()

# 1. saveVFSFile - add agent_hash
code = code.replace(
    "async function saveVFSFile(path, content) {\n            // 仅用于 config/ 虚拟目录（无后端差异）\n            const res = await fetch('/api/file?path=' + encodeURIComponent(path), {",
    "async function saveVFSFile(path, content) {\n            var hp = agentHash ? '&agent_hash=' + encodeURIComponent(agentHash) : '';\n            const res = await fetch('/api/file?path=' + encodeURIComponent(path) + hp, {"
)

# 2. saveContentFile local branch - add agent_hash
code = code.replace(
    "async function saveContentFile(path, content) {\n            // 保存文本文件\n            if (STORAGE_BACKEND === 'local') {\n                const res = await fetch('/api/file?path=' + encodeURIComponent(path), {",
    "async function saveContentFile(path, content) {\n            var hp = agentHash ? '&agent_hash=' + encodeURIComponent(agentHash) : '';\n            // 保存文本文件\n            if (STORAGE_BACKEND === 'local') {\n                const res = await fetch('/api/file?path=' + encodeURIComponent(path) + hp, {"
)

# 3. saveFile() - after save from unsaved dialog, don't close on failure
# The saveAndClose function already handles this correctly (catch doesn't close modal)
# But the direct saveFile() function uses alert() which is bad UX
# Replace alert() calls with something better
code = code.replace(
    "                alert('保存成功');\n                closeModal();\n                loadFiles();\n            } catch (e) {\n                alert('保存失败: ' + e.message);",
    "                closeModal();\n                loadFiles();\n            } catch (e) {\n                alert('保存失败: ' + e.message);"
)

# 4. Also replace config save alert
code = code.replace(
    "                    alert('配置已保存');\n                    closeModal();\n                    loadFiles();",
    "                    closeModal();\n                    loadFiles();"
)

with open('templates/agent_files.html', 'w') as f:
    f.write(code)
print("✅ saveVFSFile + saveContentFile: agent_hash fix")
