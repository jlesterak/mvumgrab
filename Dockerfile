# mvumgrab: USFS MVUM -> OsmAnd, with an offline phone app. Python stdlib on top of GDAL (the full image:
# the slim ones can't read GeoPDFs). GDAL is pinned to a release; :latest is a nightly dev build.
FROM ghcr.io/osgeo/gdal:ubuntu-full-3.13.3

WORKDIR /app
COPY mvum ./mvum
COPY docker/entrypoint.sh /entrypoint.sh

ENV PUID=1000 \
    PGID=1000 \
    TZ=Etc/UTC \
    HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    MVUM_HOST=0.0.0.0 \
    MVUM_PORT=8765 \
    MVUM_GDAL=local \
    MVUM_DATA=/config/data \
    MVUM_OUT=/config/out \
    MVUM_CATALOG=/config/data/catalog.json

VOLUME ["/config"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['MVUM_PORT'], timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "mvum.server"]
