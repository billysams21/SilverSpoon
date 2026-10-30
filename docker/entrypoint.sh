#!/bin/sh
# linuxserver.io-style PUID/PGID support: start as root, remap the
# `silverspoon` user to the requested ids, fix ownership of the volumes,
# then drop privileges. If the container is already started unprivileged
# (e.g. compose `user:`), skip all of that and just run the command.
set -e

if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID$PGID" in
  *[!0-9]*) echo "PUID and PGID must be numeric (got '$PUID' / '$PGID')" >&2; exit 1 ;;
esac

# Stale Chromium singleton locks (nothing can be running yet) would otherwise
# make the browser refuse the profile after a restart.
rm -rf /tmp/org.chromium.* /tmp/silverspoon_browser_profile/Singleton* 2>/dev/null || true

# PUID=0 means "run as root": no user remap or chown needed. A non-zero PGID
# still sets the group the process runs with.
if [ "$PUID" = "0" ]; then
  echo "Starting SilverSpoon as uid=0 gid=$PGID"
  if [ "$PGID" != "0" ]; then
    exec setpriv --regid "$PGID" --clear-groups "$@"
  fi
  exec "$@"
fi

# -o allows ids that already exist in the image (e.g. 33 = www-data)
groupmod -o -g "$PGID" silverspoon
usermod -o -u "$PUID" -g "$PGID" silverspoon

chown "$PUID:$PGID" /data /downloads /tmp/home
# settings/queue history is small; downloads can be huge, so only the top level
chown -R "$PUID:$PGID" /data
# Chromium's profile and HOME live in /tmp and survive `docker restart`; if the
# previous start used another PUID they are owned by the wrong user and the
# browser exits immediately.
chown -R "$PUID:$PGID" /tmp/home /tmp/silverspoon_browser_profile 2>/dev/null || true

# supervisord logs to /dev/stdout; the log pipe is root-owned, so hand it over
chown "$PUID:$PGID" /proc/self/fd/1 /proc/self/fd/2 2>/dev/null || true

echo "Starting SilverSpoon as uid=$PUID gid=$PGID"
exec gosu silverspoon "$@"
