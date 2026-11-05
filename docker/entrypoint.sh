#!/bin/sh
# Runs as root only long enough to give /data to PUID:PGID, then drops privileges.
set -e
mkdir -p /data
if [ "$(id -u)" = "0" ]; then
  chown -R "${PUID}:${PGID}" /data
  RUN="setpriv --reuid=${PUID} --regid=${PGID} --clear-groups"
else
  RUN=""
fi
case "$1" in
  web)   # the access log has the path without the query string and no referrer, so ?token= never lands in it
    exec $RUN gunicorn -k gthread -w 2 --threads 8 -b 0.0.0.0:8181 \
      --access-logfile - --access-logformat '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"' \
      --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
      'bingestats:create_app()' ;;
  worker)
    exec $RUN python -m bingestats worker ;;
  *)
    exec $RUN python -m bingestats "$@" ;;
esac
