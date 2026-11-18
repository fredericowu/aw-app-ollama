FROM ollama/ollama:0.40.16
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && python3 -m venv /app/venv \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN /app/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY gateway.py config-schema.json panel.html ./
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=15s --start-period=180s --retries=3 \
    CMD /app/venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/workspace/health',timeout=12)"
ENTRYPOINT ["/app/venv/bin/python", "/app/gateway.py"]
