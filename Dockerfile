FROM python:3.12-slim

WORKDIR /app

# 系统依赖 + pip 腾讯云镜像（国内加速）
RUN mkdir -p /root/.pip && \
    printf "[global]\nindex-url = https://mirrors.cloud.tencent.com/pypi/simple\n" > /root/.pip/pip.conf && \
    apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    pip install --no-cache-dir eval_type_backport && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY . .

# 数据目录
RUN mkdir -p /app/data /app/feclaw-storage

EXPOSE 8080

CMD ["python", "main.py"]
