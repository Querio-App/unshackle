FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 unshackle
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY skills ./skills
COPY spec ./spec
RUN pip install --no-cache-dir '.[mcp]' && mkdir /repo
COPY deploy/entrypoint.sh /usr/local/bin/unshackle-http
RUN chmod 755 /usr/local/bin/unshackle-http
RUN chmod 1777 /tmp && touch /tmp/.unshackle-scratch
VOLUME ["/tmp"]
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s \
  CMD python -c "import os,socket; socket.create_connection(('127.0.0.1',int(os.environ.get('PORT','8000'))),2).close()"
ENTRYPOINT ["/usr/local/bin/unshackle-http"]
