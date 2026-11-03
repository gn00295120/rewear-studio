# Build from the tested wheel, with no project data or credentials in the context.
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 rewear \
    && useradd --uid 10001 --gid rewear --no-create-home rewear \
    && install -d -o rewear -g rewear -m 700 /var/lib/rewear
COPY deploy/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes -r /tmp/requirements.lock
COPY dist/rewear_studio-1.0.0-py3-none-any.whl /tmp/rewear_studio-1.0.0-py3-none-any.whl
RUN pip install --no-deps /tmp/rewear_studio-1.0.0-py3-none-any.whl \
    && rm /tmp/requirements.lock /tmp/rewear_studio-1.0.0-py3-none-any.whl
USER 10001:10001
WORKDIR /var/lib/rewear
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.parse,urllib.request; from wardrobe.hosted import _origin; origin,_=_origin(os.environ['REWEAR_PUBLIC_ORIGIN'],False); authority=urllib.parse.urlsplit(origin).netloc; request=urllib.request.Request('http://127.0.0.1:8765/healthz',headers={'Host':authority}); assert urllib.request.urlopen(request,timeout=3).status==200"
CMD ["rewear", "--hosted", "--host", "127.0.0.1", "--port", "8765", "--hosted-data-dir", "/var/lib/rewear/data"]
