FROM nginx:1.27-alpine

RUN apk add --no-cache bash openssl gettext tini

COPY docker/edge/entrypoint.sh /entrypoint.sh
COPY docker/edge/nginx-main.conf.template /etc/nginx/templates/nginx.conf.template
COPY docker/edge/nginx.conf.template /etc/nginx/templates/default.conf.template

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
