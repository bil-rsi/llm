# Isolated shell sandbox. Build context: platform/ (so the shared rules file comes from the backend package).
FROM python:3.13-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends git file tree jq less \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10002 runner && useradd -u 10002 -g 10002 -d /home/runner -s /bin/sh runner \
    && install -d -m 1777 /run/aip && install -d -o 10002 -g 10002 /home/runner /workspace /extra
COPY backend/src/aiplatform/tools/shell/rules.py /opt/runner/rules.py
COPY tool-runner/runner.py /opt/runner/runner.py
USER 10002:10002
WORKDIR /opt/runner
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/opt/runner/runner.py"]
