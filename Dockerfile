# Runtime for runner.py + team.py on the Linux server. The code, cache/ and reports/ are the mounted repo dir (not baked in),
# so a code update = copy the files + `docker compose restart team`.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-dejavu-core openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir confluent-kafka==2.16.0
ARG UID=1000
RUN useradd -u $UID -m bot
USER bot
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
CMD ["python", "runner.py"]
