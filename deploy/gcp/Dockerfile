# Extend the existing runtime, keeping its dependencies, UID and command intact.
ARG BASE_IMAGE=unshackle-gcp-base
FROM ${BASE_IMAGE}
COPY ssh-entrypoint.sh /app/gcp-entrypoint.sh
ENTRYPOINT ["/bin/sh", "/app/gcp-entrypoint.sh"]
