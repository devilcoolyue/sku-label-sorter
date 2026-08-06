FROM python:3.11-slim

# onnxruntime(OCR 兜底) 需要 libgomp；其余依赖的 wheel 都是自包含的。
# 两处适配国内网络：deb.debian.org 会解析到 IPv6，而容器没有 IPv6 出口，
# apt 会死等超时 —— 所以强制 IPv4 并换成阿里云镜像。
RUN echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4 \
    && sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
         /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 国内机器直连 pypi 很慢，走阿里云镜像
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir \
        pymupdf fastapi uvicorn python-multipart \
        openai anthropic \
        rapidocr-onnxruntime opencv-python-headless

WORKDIR /app
COPY label_sorter/ /app/label_sorter/

# 任务目录挂出去，容器重建后历史记录还在
ENV SORTER_WORK_DIR=/data \
    PORT=8000
VOLUME /data

WORKDIR /app/label_sorter
EXPOSE 8000

# 必须单进程：任务状态存在进程内存的 JOBS 字典里，多 worker 会互相看不见
CMD ["python", "app.py"]
