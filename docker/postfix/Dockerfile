FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       postfix \
       postfix-pcre \
       postfix-pgsql \
       gettext-base \
       ca-certificates \
       openssl \
       tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r tempmail && useradd -r -g tempmail -d /nonexistent -s /usr/sbin/nologin tempmail

WORKDIR /app

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api/app /app/tempmail
COPY docker/postfix/entrypoint.sh /entrypoint.sh
COPY config/postfix /config/postfix

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["postfix", "start-fg"]
