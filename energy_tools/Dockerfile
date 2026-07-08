# Supervisor builds pass BUILD_FROM=<ha base image>; a plain `docker build`
# gets stock alpine, which works because run.sh guards the s6 env loading.
ARG BUILD_FROM=alpine:3.21
FROM ${BUILD_FROM}

# tzdata: foxctl's tariff windows use naive local time — TZ must resolve.
RUN apk add --no-cache python3 py3-pip tzdata
RUN pip3 install --no-cache-dir --break-system-packages paho-mqtt websocket-client

COPY foxctl.py foxess_probe.py nemfuel.py build_config.py foxctl_config.json nemfuel_config.json run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
