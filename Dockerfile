# AMP Community Edition (experimental v0) — self-contained, minimal dependencies.
FROM python:3.12-slim

WORKDIR /opt/amp
COPY . ./src/amp-community
# amp-core must be present next to this build context, or pre-installed.
RUN pip install --no-cache-dir ./src/amp-community/vendor/amp-core ./src/amp-community 2>/dev/null \
 || pip install --no-cache-dir ./src/amp-community

VOLUME /data
ENV AMP_COMMUNITY_CONFIG=/data/config.json
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "-m", "amp_community.api", "--host", "0.0.0.0"]
