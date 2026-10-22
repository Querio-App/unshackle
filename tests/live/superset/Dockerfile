# apache/superset 6.x images ship without database drivers; add the Postgres one.
FROM apache/superset:6.1.0
USER root
RUN uv pip install --python /app/.venv/bin/python --no-cache psycopg2-binary
USER superset
