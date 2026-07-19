FROM python:3.12-slim

WORKDIR /app

# 系统依赖（不含 FUSE，需要时另加）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && pip install --no-cache-dir eval_type_backport \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY . .

# 创建数据目录
RUN mkdir -p feclaw-storage feclaw-fuse

EXPOSE 8080

# 默认启动（可通过 CMD 覆盖）
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
