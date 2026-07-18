#!/usr/bin/env bash
# FeClaw 开发环境初始化脚本
# 用法: bash scripts/dev_init.sh
set -e

echo "=== FeClaw 开发环境初始化 ==="

# 1. Check Python
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0")
if [ "$(echo "$PY_VER >= 3.12" | bc -l 2>/dev/null)" != "1" ]; then
    echo "❌ Python 3.12+  required (found $PY_VER)"
    echo "  安装: sudo apt install python3 python3-pip python3-venv"
    exit 1
fi
echo "✅ Python $PY_VER"

# 2. Check/Setup MySQL
if command -v mysql &>/dev/null; then
    echo "✅ MySQL 已安装"
else
    echo "⚠️  MySQL 未安装"
    if command -v docker &>/dev/null; then
        echo "   正在用 Docker 启动临时 MySQL 实例..."
        docker rm -f feclaw-dev-mysql 2>/dev/null || true
        docker run -d --name feclaw-dev-mysql \
            -e MYSQL_ROOT_PASSWORD=feclaw_dev \
            -e MYSQL_DATABASE=FeClaw \
            -p 3306:3306 \
            mysql:8 --default-authentication-plugin=mysql_native_password
        echo "   MySQL 启动中（等待 10 秒...）"
        sleep 10
        echo "   MySQL URL: mysql+pymysql://root:feclaw_dev@localhost:3306/FeClaw"
    else
        echo "❌ 请先安装 MySQL 或 Docker"
        echo "   apt install mysql-server"
        echo "   或: docker run -d -p 3306:3306 -e MYSQL_ROOT_PASSWORD=dev mysql:8"
        exit 1
    fi
fi

# 3. Create venv + install deps
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv venv
fi
source venv/bin/activate
echo "📦 安装 Python 依赖..."
pip install -r requirements.txt -q
if [ -f requirements-fuse.txt ]; then
    echo "📦 （可选）安装 FUSE 依赖..."
    pip install -r requirements-fuse.txt -q 2>/dev/null || true
fi

# 4. Setup .env if needed
if [ ! -f ".env" ]; then
    echo "🔑 生成 .env..."
    JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > .env << ENVEOF
JWT_SECRET=$JWT_SECRET
DATABASE_URL=mysql+pymysql://root:feclaw_dev@localhost:3306/FeClaw
QWEN_API_KEY=
DEEPSEEK_API_KEY=
DEBUG=True
COOKIE_SECURE=False
MAIN_TEXT_MODEL=deepseek-v4-flash
MAIN_VISION_MODEL=qwen3.6-35b-a3b
MAIN_EMBEDDING_MODEL=text-embedding-v4
VECTOR_STORAGE_BACKEND=numpy
STORAGE_MODE=local
FUSE_ENABLED=false
ENVEOF
    # 修复权限（dev_init.sh 可能用 sudo 运行）
    sudo chown $(whoami) .env 2>/dev/null || true
    echo "✅ .env 已生成（需填入 API Key）"
else
    echo "✅ .env 已存在"
fi

# 5. Start
echo ""
echo "================================="
echo " 🚀 启动: source venv/bin/activate && python main.py"
echo " 或: uvicorn main:app --host 0.0.0.0 --port 8080"
echo "================================="
