# The explorer for a team, on your own infrastructure.
#
#   docker build -t dax-lineage-explorer .
#   docker run --rm -p 127.0.0.1:8765:8765 dax-lineage-explorer
#   # then open http://127.0.0.1:8765
#
# Behind an internal name, say which:
#   docker run --rm -p 8765:8765 dax-lineage-explorer \
#     daxlin serve --host 0.0.0.0 --no-open --allow-host lineage.corp.example
#
# The container serves one static page. Files people open in it are read in
# their own browser and never reach the container.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY dax_lineage ./dax_lineage
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --no-create-home daxlin

USER daxlin
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/')"
CMD ["daxlin", "serve", "--host", "0.0.0.0", "--port", "8765", "--no-open"]
