# 模型选择指南

## 可用模型

spawn_subagent 支持以下模型：

| 模型名 | 能力 | 适用场景 |
|--------|------|----------|
| `doubao-seed-2-0-lite-260215` | 轻量、快速、便宜 | 文字提取、简单任务 |
| `doubao-seed-2-0-pro-260215` | 强、准确、较慢 | 解题、代码审查、深度分析 |

## 模型选择原则

### 用 `lite`（轻量模型）的场景
- 从图片中提取文字（OCR）
- 简单分类、摘要
- 格式转换
- 不涉及复杂推理的辅助任务
- **model="auto" 默认使用 lite**

### 用 `pro`（高性能模型）的场景
- 数学题解答（需要严谨推理）
- GeoGebra 命令生成
- 代码审查和分析
- 需要深度思考的复杂任务
- 对准确性要求高的场景

## reasoning_effort 说明

| 值 | 效果 | 适用 |
|----|------|------|
| `"high"` | 开启深度思考 | 复杂推理任务（解题、代码分析） |
| `"off"` | 显式关闭 | 简单任务、纯提取（推荐） |
| 不传 | 默认关闭 | 一般任务 |

## 调用示例

### 文字提取（简单任务，用 lite）
```python
spawn_subagent(
    model="doubao-seed-2-0-lite-260215",
    reasoning_effort="off",
    task="提取图片中的所有文字",
    image_path="workspace/images/xxx.png"
)
```

### 数学解题（复杂任务，用 pro + 深度思考）
```python
spawn_subagent(
    model="doubao-seed-2-0-pro-260215",
    reasoning_effort="high",
    task="解答图片中的数学题，给出详细步骤",
    image_path="workspace/images/xxx.png"
)
```

## 注意事项

- `model="auto"` 默认使用 `lite` 模型，如果需要更强的推理能力，请显式指定 `model="doubao-seed-2-0-pro-260215"`
- 对于复杂任务，建议搭配 `reasoning_effort="high"` 使用 pro 模型
- 对于纯提取类任务，建议设置 `reasoning_effort="off"` 以加速响应
