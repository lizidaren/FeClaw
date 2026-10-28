# FeClaw 生产镜像（T19a：依赖只从 requirements.lock 精确安装）
FROM python:3.12.9-slim

WORKDIR /app

# 系统依赖 + pip 腾讯云镜像（国内加速）
RUN mkdir -p /root/.pip && \
    printf "[global]\nindex-url = https://mirrors.cloud.tencent.com/pypi/simple\n" > /root/.pip/pip.conf && \
    apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# 依赖安装：唯一来源是 requirements.lock（requirements.txt 只是范围声明层）。
#   * lock 含 core / optional / fuse / test / transitive 全部分组，等于已实测矩阵；
#     想缩到 core+optional 也可以（见 lock 头部“Core-only install”），但必须重跑测试。
#   * 不用 `--no-deps`：lock 是从 Windows venv 冻结的，Linux 专属 extra（如
#     uvicorn[standard] 的 uvloop）不在其中，需要让 resolver 按平台补齐。
#   * 暂未启用 `--require-hashes`：lock 还没有 hash 行（补 wheel 缓存后属 T19b）。
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# FUSE 挂载（可选功能）默认**不**安装：需要系统层 fuse3 + libfuse3-dev，
# 并在运行时加 --device /dev/fuse --cap-add SYS_ADMIN。要开启时追加：
#   COPY requirements-fuse.txt .
#   RUN apt-get update && apt-get install -y --no-install-recommends fuse3 libfuse3-dev \
#       && pip install --no-cache-dir -r requirements-fuse.txt

# 应用代码
COPY . .

# 数据目录
RUN mkdir -p /app/data /app/feclaw-storage

EXPOSE 8080

CMD ["python", "main.py"]
