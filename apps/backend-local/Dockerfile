FROM python:3.12-slim

# libglib2.0-0 required by opencv-python-headless; git for yolov5 clone
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgl1 libsm6 libxrender1 libxext6 git wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only torch first — prevents pip from pulling multi-GB CUDA libraries
RUN pip install --no-cache-dir torch torchvision \
    --index-url https://download.pytorch.org/whl/cpu

# Install remaining deps (torch/torchvision already satisfied above)
COPY apps/backend-local/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# YOLOv5 source — cloned at build time, not copied from the repo
# Install its requirements too (excluding torch/torchvision already installed above)
RUN git clone --depth 1 --branch v7.0 \
    https://github.com/ultralytics/yolov5 /app/yolov5 && \
    grep -vE '^(torch|torchvision)' /app/yolov5/requirements.txt | \
    pip install --no-cache-dir -r /dev/stdin

# Backend application (weights downloaded separately — not stored in git)
COPY apps/backend-local/ ./backend-local/
RUN mkdir -p ./backend-local/weights && \
    wget -q -O ./backend-local/weights/yolov5s.pt \
    https://github.com/llravelo/patchguard-erp/releases/download/v0.1/yolov5s.pt

ENV MODEL_WEIGHTS=/app/backend-local/weights/yolov5s.pt \
    YOLOV5_DIR=/app/yolov5 \
    INFER_DEVICE=cpu \
    PORT=8000

WORKDIR /app/backend-local
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
