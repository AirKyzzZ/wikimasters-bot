FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/*

RUN useradd --create-home --uid 1000 wmbot && mkdir /data && chown wmbot /data
USER wmbot
ENV WM_DATA_DIR=/data WM_SESSION_FILE=/data/session.json PYTHONUNBUFFERED=1
VOLUME /data

ENTRYPOINT ["wmbot"]
CMD ["run"]
