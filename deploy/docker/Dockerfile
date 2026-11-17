# The team workspace with Postgres support. Built by docker-compose.yml next to this file.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY dax_lineage ./dax_lineage
RUN pip install --no-cache-dir ".[postgres]" && useradd --create-home workspace
USER workspace
EXPOSE 8766
CMD ["python", "-m", "dax_lineage", "workspace", "serve", "--help"]
