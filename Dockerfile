# Dockerfile - 钉钉AD同步系统
FROM python:3.11-slim

WORKDIR /app

# 切换 Debian 源到清华镜像（国内加速）
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends samba-common-bin && \
    rm -rf /var/lib/apt/lists/*

# 直接安装Python依赖（ldap3是纯Python库，不需要gcc/libldap2-dev）
# 使用清华镜像源加速
COPY backend/requirements.txt .
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    -r requirements.txt

# 复制后端代码
COPY backend/ .

# 复制前端静态文件
COPY frontend/ ./static/

# 创建数据和日志目录
RUN mkdir -p /app/data /app/logs

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
