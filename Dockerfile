FROM lscr.io/linuxserver/baseimage-alpine:3.19

ENV PUID=99 \
    PGID=100 \
    UMASK=002

RUN apk add --no-cache python3 py3-pip gcc python3-dev musl-dev ncurses su-exec

COPY . /app/
RUN set -eux; cd /app; \
    pip3 install --no-cache-dir --break-system-packages .; \
    apk del gcc python3-dev musl-dev; rm -rf /tmp/* /root/.cache

# wrapper: drop to 99:100 (nobody:nogroup) explicitly
RUN <<'EOF'
cat > /usr/local/bin/rip <<'SCRIPT'
#!/bin/sh
set -e
export HOME=/config
cd /downloads
exec su-exec ${PUID:-99}:${PGID:-100} env HOME=/config /usr/bin/rip "$@"
SCRIPT
chmod +x /usr/local/bin/rip
EOF

# single job script used both at startup and daily; imports env via s6
RUN mkdir -p /etc/periodic/daily && \
    printf '%s\n' \
      '#!/usr/bin/with-contenv sh' \
      'set -e' \
      'export HOME=/config' \
      '/usr/local/bin/rip url https://tidal.com/my-collection/albums https://tidal.com/my-collection/artists https://tidal.com/my-collection/tracks https://www.deezer.com/en/profile/6629952041/artists https://www.deezer.com/en/profile/6629952041/albums https://www.deezer.com/en/profile/6629952041/loved https://play.qobuz.com/user/library/favorites/albums https://play.qobuz.com/user/library/favorites/artists https://play.qobuz.com/user/library/favorites/tracks' \
      'date' \
      > /etc/periodic/daily/rip-tidal && \
    chmod +x /etc/periodic/daily/rip-tidal

# run once on container start
RUN mkdir -p /etc/cont-init.d && \
    ln -s /etc/periodic/daily/rip-tidal /etc/cont-init.d/99-run-rip-once

# cron setup (4 AM EDT daily)
RUN printf '%s\n' \
      'SHELL=/bin/sh' \
      'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
      '0 4 * * * /etc/periodic/daily/rip-tidal' \
      > /etc/crontabs/root

# s6 service keeps crond in foreground (container stays alive)
RUN mkdir -p /etc/services.d/crond && \
    printf '%s\n' \
      '#!/usr/bin/with-contenv sh' \
      'exec crond -f -l 8' \
      > /etc/services.d/crond/run && \
    chmod +x /etc/services.d/crond/run

VOLUME /home /downloads
WORKDIR /downloads
# no CMD (LSIO uses /init)
