FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash jq git curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest==8.4.2 pytest-timeout==2.4.0 pyyaml==6.0.2 \
    && useradd --uid 10001 --create-home --shell /bin/bash bench \
    && mkdir -p /shared/workspace /shared/reports /shared/submission /task /grader \
    && ln -s /shared/workspace /workspace \
    && ln -s /shared/reports /reports \
    && ln -s /shared/submission /submission

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp
USER 10001:10001
WORKDIR /tmp
CMD ["sleep", "infinity"]
