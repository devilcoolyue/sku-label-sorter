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

# rapidocr 声明的依赖是 opencv-python（带 GUI 的那个），于是它和 headless 会被
# 同时装上 —— 两者提供同一个 cv2 模块，后装的赢，结果 cv2 落到需要 X11 的那份，
# 在 slim 镜像里一 import 就 ImportError: libxcb.so.1。
# 与其补装一堆图形库，不如让 cv2 只由 headless 提供。两个一起卸干净再装回
# headless，是因为它们共用同一个 cv2 目录，只卸其中一个会留下残缺文件。
RUN pip uninstall -y opencv-python opencv-python-headless \
    && pip install --no-cache-dir opencv-python-headless \
    && python -c "import cv2; from rapidocr_onnxruntime import RapidOCR; RapidOCR()"

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
