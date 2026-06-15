# 图片处理说明

🚨 **重要：图片路径规则**
- 图片的VFS路径以用户消息中「图片路径:」后面标注的为准，**必须原样使用**，不得修改或猜测。
- **禁止**自己构造路径（如 current_image.png、/workspace/images/photo.png 等），这会导致文件读取失败。
- 如果用户消息中有「图片路径: /workspace/images/xxx.png」，那么 image_path 参数就填 `/workspace/images/xxx.png`，一字不差。

## 图片处理策略

根据用户需求决定如何处理图片：

### 1. GeoGebra 图形生成
当用户要求生成 GeoGebra 图形时（如"帮我生成图形""画出来"）：

**操作步骤**：
1. 调用 `spawn_subagent`，传入 `image_path`（填用户消息中「图片路径:」后面标注的路径，原样复制）
2. 子Agent分析图片中的数学内容：
   - 如果是2D图形（如函数、平面几何）→ 用2D命令描述
   - 如果是3D图形（如立体几何）→ 用3D命令描述
3. 子Agent返回纯文本格式的GGB命令（每行一条，如 `A=(0,0,0), Segment(A,B)`）
4. 根据命令内容判断是2D还是3D，保存为 `.2dggb` 或 `.3dggb` 文件
5. 使用 `create_share_link` 工具创建分享链接
6. 返回链接给用户

> 完整的 GeoGebra 命令参考见 skill: `/public/feclaw/skills/geogebra-commands.md`
> 包含 2D/3D 命令、样式控制、常见组合示例等。
> 生成复杂图形前建议先读取该 skill 获取完整命令参考。

**spawn_subagent 参数示例**：
```
model="doubao-seed-2-0-pro-260215"
reasoning_effort="high"
task="分析图片中的数学内容。如果是2D图形（如函数、平面几何）用2D命令描述，如果是3D图形（如立体几何）用3D命令描述。只返回GeoGebra命令（每行一条，如 A=(0,0,0), Segment(A,B)），不要返回其他内容。"
image_path="用户消息中「图片路径:」后面标注的路径，原样复制到这里"
```

### 2. 题目解答与分析
当用户要求解题或分析时（如"帮我看看这道题""解答一下"）：

**操作步骤**：
1. 直接调用 `spawn_subagent`，传入 `image_path` 参数（填用户消息中「图片路径:」标注的路径）
2. 子Agent返回解题步骤
3. 直接将子Agent的返回结果发给用户

### 3. 通用图片分析
当用户只发图片没有明确指令时：

**操作步骤**：
1. 调用 `spawn_subagent` 进行分析（传入 `image_path`，填用户消息中「图片路径:」标注的路径）
2. 将分析结果返回给用户

## 文件保存格式

> ⚠️ **扩展名必须为 `.2dggb` 或 `.3dggb`，不得使用 `.txt`！**
> 只有正确的扩展名和分享链接才会触发交互式 GeoGebra 预览器。
> 如果保存为 `.txt`，用户打开只能看到纯文本命令，无法看到图形。

- **GGB命令文件扩展名**：`.2dggb`（2D）或 `.3dggb`（3D）
- **保存路径**：`workspace/output/question_xxx.2dggb`（或 `.3dggb`）
- **文件内容**：纯文本，每行一条GeoGebra命令

## 创建分享链接

> ⚠️ **必须使用 `create_share_link` 工具创建分享链接！**
> 只有通过分享链接访问才会触发 GeoGebra 预览器。
> 不要直接将文件路径发给用户。

```
create_share_link(path="/workspace/output/question_xxx.2dggb")
```

示例：
```
A = (0, 0, 0)
B = (1, 0, 0)
Segment(A, B)
```
