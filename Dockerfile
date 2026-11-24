FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖仍以 requirements.txt 为唯一清单；容器里去掉桌面 GUI 依赖（PyQt6 / pywin32）
COPY requirements.txt ./
RUN grep -v -E '^(PyQt6|pywin32)' requirements.txt > /tmp/requirements-web.txt \
    && pip install --no-cache-dir -r /tmp/requirements-web.txt \
    && pip install --no-cache-dir "PyMySQL>=1.1" \
    && rm -f /tmp/requirements-web.txt

COPY . .

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "web.query_ui.app:app", "--host", "0.0.0.0", "--port", "8770"]
