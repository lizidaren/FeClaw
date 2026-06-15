# FeClaw Skills

Skills 是按需加载的能力引导文件。当你遇到以下场景时，查阅对应的 Skill 文件：

## 可用 Skills

| Skill | 触发关键词 | VFS 路径 | 说明 |
|:------|:-----------|:---------|:------|
| 错题整理 | 错题、错题本、整理错题、记录错题 | `/public/feclaw/skills/mistake-notebook.md` | 帮用户整理拍照/手写的错题 |
| 单词本 | 单词、生词、词汇、不认识、单词本 | `/public/feclaw/skills/vocabulary-notebook.md` | 帮用户收集整理不认识的单词，支持图片提取和文字录入 |
| GeoGebra 图形 | 画图、生成图形、GGB、GeoGebra、几何图形、画出、作图、图形生成 | `/public/feclaw/skills/geogebra-commands.md` | 根据图片或文字描述生成 GeoGebra 交互式图形，支持 2D/3D |
| **App 开发** | **网页、网站、上线、部署、发布网站、Web App** | `/public/feclaw/skills/app-system.md` | 创建、注册和发布 Web 应用，支持静态页面、AI 端点和代码端点 |

## 使用方式

当用户表达的需求与上表触发关键词匹配时：
1. 读取对应的 Skill 文件（使用完整 VFS 路径，如 `cat /public/feclaw/skills/mistake-notebook.md`）
2. 按文件中的步骤执行
3. 完成后告知用户
