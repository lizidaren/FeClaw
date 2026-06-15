#!/bin/bash
# Deploy FeClaw to remote server

set -e

SERVER="YOUR_SERVER_IP"  # 替换为你的服务器 IP
USER="root"
PASSWORD="YOUR_PASSWORD"  # 替换为你的服务器密码
REMOTE_DIR="/root/FeClaw"
LOCAL_DIR="/path/to/FeClaw"

echo "=== 部署 FeClaw 到远程服务器 ==="

# 使用 rsync 同步代码（排除不需要的文件）
echo "1. 同步代码..."
rsync -avz --progress \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude 'data/*.db' \
    --exclude 'logs/*' \
    --exclude '.venv' \
    $LOCAL_DIR/ $USER@$SERVER:$REMOTE_DIR/

echo "2. 重启服务..."
ssh $USER@$SERVER << 'EOF'
    pkill -f "uvicorn main:app" || true
    sleep 2
    cd /root/FeClaw
    nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8080 > /tmp/feclaw.log 2>&1 &
    sleep 3
    curl -s http://localhost:8080/health | head -20
EOF

echo "3. 验证 CSS 文件..."
curl -I https://feclaw.chat/static/css/base.css

echo "=== 部署完成 ==="
