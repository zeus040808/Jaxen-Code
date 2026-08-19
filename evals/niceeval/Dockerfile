FROM python:3.13.7-slim-bookworm

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hh-agent
COPY hh_agent_pro/ /opt/hh-agent/hh_agent_pro/

ENV PYTHONPATH=/opt/hh-agent \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir pytest==8.4.2

RUN python3 -c "import hh_agent_pro; print('hh-agent image ready')" \
    && git --version \
    && rg --version
