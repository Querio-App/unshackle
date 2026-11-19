FROM postgres:16-alpine
COPY schema.sql /docker-entrypoint-initdb.d/10-schema.sql
COPY reader.sh /docker-entrypoint-initdb.d/20-reader.sh
RUN chmod 755 /docker-entrypoint-initdb.d/20-reader.sh
