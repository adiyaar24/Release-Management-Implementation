FROM python:3.12-alpine

LABEL maintainer="Elevance / Harness"
LABEL description="Drone plugin: one PR per YAML under a path (newline append), branches release/<ticket>-<service>"
LABEL version="1.0.0"

RUN apk add --no-cache git

WORKDIR /app

COPY drone_pr_plugin.py ./

RUN printf '%s\n' \
  'import sys' \
  'sys.path.insert(0, "/app")' \
  'from drone_pr_plugin import main' \
  'raise SystemExit(main())' \
  > /usr/local/bin/plugin.py

RUN mkdir -p /harness

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PLUGIN_WORK_DIR=/harness

ENTRYPOINT ["python", "/usr/local/bin/plugin.py"]
