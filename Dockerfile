FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99 \
    HOME=/tmp/home \
    SILVERSPOON_DATA_DIR=/data \
    SILVERSPOON_DOWNLOAD_DIR=/downloads

# chromium: the CAPTCHA solver's browser (found via `chromium` on PATH)
# 7zip:     provides 7zz, used for archive extraction
# xvfb, x11vnc, novnc, websockify: virtual display plus its browser view, so a
#           Cloudflare check that needs a human click can be solved from the web UI
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium 7zip fonts-liberation \
      xvfb x11vnc novnc websockify supervisor gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-web.txt .
RUN pip install -r requirements-web.txt

COPY engine.py cf_turnstile.py providers.py segmented.py extraction.py scheduler.py ./
COPY webapp ./webapp
COPY docker/supervisord.conf /etc/supervisor/conf.d/silverspoon.conf
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# The entrypoint starts as root, remaps this user to PUID/PGID (default 1000),
# fixes ownership of the volumes and drops privileges with gosu. Setting
# `user:` instead skips the remap and runs directly as that uid.
ENV PUID=1000 PGID=1000
RUN useradd --uid 1000 --create-home silverspoon \
 && mkdir -p /data /downloads /tmp/home /tmp/.X11-unix \
 && chmod 1777 /tmp/.X11-unix /tmp/home \
 && chown silverspoon /data /downloads

VOLUME ["/data", "/downloads"]
EXPOSE 8080 6080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/state', timeout=4)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/silverspoon.conf"]
