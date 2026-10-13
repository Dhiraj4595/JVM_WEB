FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1 \
    JVM_TRUST_PROXY=1 \
    JVM_DB_PATH=/data/school.sqlite3 \
    PORT=8000
# Mount a persistent volume at /data so student edits survive restarts and redeploys.
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8000'))"
CMD ["python", "app_server.py"]
