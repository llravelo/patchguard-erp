FROM python:3.12-slim

# libglib2.0-0 required by opencv-python-headless; git for yolov5 clone
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libsm6 libxrender1 libxext6 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so this layer is cached on code-only changes
COPY apps/backend-local/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# YOLOv5 source — cloned at build time, not copied from the repo
RUN git clone --depth 1 --branch v7.0 \
    https://github.com/ultralytics/yolov5 /app/yolov5

# Backend application
COPY apps/backend-local/ ./backend/

ENV MODEL_WEIGHTS=/app/backend/weights/yolov5s.pt \
    YOLOV5_DIR=/app/yolov5 \
    INFER_DEVICE=cpu \
    PORT=8000

WORKDIR /app/backend
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
