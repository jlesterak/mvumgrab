#!/bin/sh
# Run as PUID:PGID (DockSTARTer convention) and make sure the data folders exist and are ours.
set -e
mkdir -p "$MVUM_DATA" "$MVUM_OUT"
if [ "$(id -u)" = 0 ]; then
  chown "$PUID:$PGID" /config "$MVUM_DATA" "$MVUM_OUT"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
