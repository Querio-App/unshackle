ARG BASE_IMAGE=unshackle-aws-base:local
FROM ${BASE_IMAGE}
USER root
COPY start.py /usr/local/bin/unshackle-aws
COPY ssh /usr/local/bin/ssh
RUN chmod 755 /usr/local/bin/unshackle-aws /usr/local/bin/ssh
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/unshackle-aws"]
