FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m -u 10001 procure && mkdir /data && chown procure:procure /data
COPY --chown=procure:procure . .
USER procure
ENV PROCURE_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3)"
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000","--workers","1","--no-server-header"]
