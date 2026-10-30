FROM python:3.12.16-slim@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && if ! getent group "${APP_GID}" >/dev/null; then groupadd --gid "${APP_GID}" arepago; fi \
    && if ! getent passwd "${APP_UID}" >/dev/null; then \
         useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin arepago; \
       fi

COPY --chown=${APP_UID}:${APP_GID} services /app/services

USER ${APP_UID}:${APP_GID}
