# 🐳 Docker 部署

## 快速开始（推荐）

```bash
git clone https://github.com/lizidaren/FeClaw.git
cd FeClaw

# 1. 填入 LLM API Key
#    编辑 docker-compose.yml，找到 DEEPSEEK_API_KEY / QWEN_API_KEY 等，填入对应 Key

# 2. 启动
docker compose up -d

# 3. 获取冷启动 token
docker logs feclaw-app 2>&1 | grep "SETUP_TOKEN"
# 输出: SETUP_TOKEN=7492f8a05b3c414369b0910bdf8dcd86

# 4. 打开浏览器访问
#    http://localhost:8080/setup?token=7492f8a05b3c414369b0910bdf8dcd86
#    设置 admin 密码 → 确认模型 → 完成
```

## 生产部署

```bash
# 修改 docker-compose.yml 中的环境变量
# - JWT_SECRET：设置一个强随机密钥
# - DEBUG：设为 false
# - COOKIE_SECURE：设为 true（配合 HTTPS）

# 重建并启动
docker compose up -d --build
```

## 使用外部 MySQL

如果已有 MySQL 实例，改为 `docker compose up -d feclaw` 单独运行 FeClaw：

```yaml
services:
  feclaw:
    environment:
      DATABASE_URL: mysql+pymysql://user:password@host:3306/FeClaw
```

## 启用 FUSE（文件系统挂载）

```yaml
services:
  feclaw:
    build:
      args:
        INSTALL_FUSE: "true"
    cap_add:
      - SYS_ADMIN
    devices:
      - /dev/fuse
    environment:
      FUSE_ENABLED: "true"
```

> ⚠️ FUSE 需要 `--privileged` 或特定 cap_add，不建议在共享主机上使用。

## 容器管理

```bash
# 查看日志
docker logs -f feclaw-app

# 重启
docker compose restart feclaw

# 停止
docker compose down

# 重建（更新代码后）
docker compose up -d --build feclaw
```

## 存储

- MySQL 数据：`mysql_data` 卷（持久化数据库）
- 文件存储：`feclaw_storage` 卷（用户上传的文件/Agent 工作区）

数据卷在 `docker compose down` 时不会删除。如需彻底清理：

```bash
docker compose down -v
```
