FROM registry.access.redhat.com/ubi8/python-38:latest

ARG PIPENV_DEV=False

ENV LC_ALL=en_US.UTF-8 \
    LANG=en_US.UTF-8 \
    PIP_NO_CACHE_DIR=off \
    ENABLE_PIPENV=true \
    DISABLE_MIGRATE=true \
    DJANGO_READ_DOT_ENV_FILE=false

USER root

# Copy application files to the image.
COPY . /tmp/src/.


RUN /usr/bin/fix-permissions /tmp/src && \
chmod 755 $STI_SCRIPTS_PATH/assemble $STI_SCRIPTS_PATH/run

RUN groupadd -g 1000 koku \
    && useradd -m -s /bin/bash -g 1000 -u 1000 -G root koku \
    && chmod g+rwx /opt

USER 1000

EXPOSE 8080

RUN $STI_SCRIPTS_PATH/assemble

# Set the default CMD
CMD $STI_SCRIPTS_PATH/run
