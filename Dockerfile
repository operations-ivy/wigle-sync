# Built and pushed by brick9000/build-image in brick-cicd-config:
#   build-image https://github.com/operations-ivy/wigle-sync main whitepatrick/wigle-sync:<version>
#
# Reproducible: the base image is pinned by digest, dependencies come from
# poetry.lock, and build-image sets SOURCE_DATE_EPOCH to the commit time and
# rewrites file timestamps, so a commit always builds the same image digest.
# The nightly verify-images Jenkins job rebuilds the published image and checks
# that. Bump the base deliberately, with the digest from:
#   docker buildx imagetools inspect python:3.11-slim-bookworm
ARG PYTHON=python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b

# Poetry installs the locked dependencies into /venv; only /venv is kept.
FROM ${PYTHON} AS deps
ARG POETRY_VERSION=2.1.2
# Default 15s read timeout is too tight under qemu emulation on the dev
# laptop's arm64 buildx runs.
ENV PIP_DEFAULT_TIMEOUT=120 POETRY_NO_INTERACTION=1 VIRTUAL_ENV=/venv PATH=/venv/bin:$PATH
# One package at a time: the parallel installer races on the umask, and now and
# then a file comes out mode 0666 instead of 0644, changing the image digest.
ENV POETRY_INSTALLER_PARALLEL=false
RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"
WORKDIR /code
COPY pyproject.toml poetry.lock /code/
RUN python -m venv --without-pip /venv && poetry install --no-root --only main

FROM ${PYTHON}
COPY --from=deps /venv /venv
ENV VIRTUAL_ENV=/venv PATH=/venv/bin:$PATH PYTHONPATH=/code
WORKDIR /code

COPY config.py observability.py pi_client.py wigle_client.py sync.py /code/

ENTRYPOINT ["python", "sync.py"]
