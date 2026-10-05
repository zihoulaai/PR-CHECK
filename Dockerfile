# 单进程镜像（uv 优先，回退 pip）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先拷贝依赖清单以利用层缓存
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app
COPY static ./static
COPY .env.example ./.env.example

EXPOSE 8000

# 单进程；gunicorn/uvicorn 可选
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
