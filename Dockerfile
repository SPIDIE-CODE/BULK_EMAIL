FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8080 DATABASE_PATH=/data/dashboard.sqlite3
WORKDIR /app
COPY server.py ./
COPY templates ./templates
RUN mkdir -p /data && useradd --system --uid 10001 app && chown -R app:app /app /data
USER app
EXPOSE 8080
VOLUME ["/data"]
CMD ["python", "server.py"]
