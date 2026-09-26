ARG NGINX_IMAGE=nginx:1.27-alpine

FROM ${NGINX_IMAGE}
COPY default.conf /etc/nginx/conf.d/default.conf
COPY active.conf /etc/nginx/bluegreen/active.conf
EXPOSE 80
