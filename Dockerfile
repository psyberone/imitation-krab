FROM python:3.14.8-slim-trixie@sha256:89fb7d3da20043c370643435258bdd7ab755d326d359001d02988ed15ae5219e

RUN groupadd --gid 10001 krab \
    && useradd --uid 10001 --gid krab --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin krab \
    && install -d -o krab -g krab -m 0700 \
        /var/lib/imitation-krab/data \
        /var/lib/imitation-krab/secrets \
        /var/lib/imitation-krab/backups \
    && install -o krab -g krab -m 0600 /dev/null \
        /var/lib/imitation-krab/data/.volume-initialized \
    && install -o krab -g krab -m 0600 /dev/null \
        /var/lib/imitation-krab/secrets/.volume-initialized \
    && install -o krab -g krab -m 0600 /dev/null \
        /var/lib/imitation-krab/backups/.volume-initialized \
    && install -d -o root -g root -m 0755 /opt/imitation-krab

COPY src /opt/imitation-krab/src

ENV HOME=/nonexistent \
    KRAB_DB=/var/lib/imitation-krab/data/krab.db \
    KRAB_PEPPER_FILE=/var/lib/imitation-krab/secrets/pepper.key \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/imitation-krab/src \
    PYTHONUNBUFFERED=1

USER krab:krab
WORKDIR /opt/imitation-krab

VOLUME ["/var/lib/imitation-krab/data", "/var/lib/imitation-krab/secrets"]
EXPOSE 8765
STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "-m", "imitation_krab"]
CMD ["serve", "--container-bind", "--port", "8765"]
