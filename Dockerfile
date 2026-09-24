# docker build -t whitepatrick/wigle-sync:0.1.2 . --no-cache
FROM python:3.11-slim-bookworm

ENV POETRY_VERSION=1.3.2

RUN pip install "poetry==$POETRY_VERSION"

ENV POETRY_NO_INTERACTION=1
# Default 15s read timeout is too tight under qemu emulation on the dev
# laptop's arm64 buildx runs.
ENV PIP_DEFAULT_TIMEOUT=120

WORKDIR /code

COPY pyproject.toml poetry.lock /code/

ENV PYTHONPATH=/code

RUN /usr/local/bin/poetry install --no-root --without dev

COPY config.py observability.py pi_client.py wigle_client.py sync.py /code/

ENTRYPOINT ["/usr/local/bin/poetry", "run", "python", "sync.py"]
