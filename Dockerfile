FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPERFORGE_ALLOW_SYSTEM_PYTHON=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates chktex git latexmk poppler-utils \
    texlive-bibtex-extra texlive-fonts-recommended texlive-latex-base \
    texlive-latex-extra && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system paperforge && useradd --system --gid paperforge --create-home paperforge

WORKDIR /opt/paperforge

COPY requirements-core.txt requirements-writeup.txt /opt/paperforge/
RUN python -m pip install -U pip setuptools wheel && \
    python -m pip install -r /opt/paperforge/requirements-writeup.txt

COPY . /opt/paperforge
RUN python -m pip install --no-deps /opt/paperforge && \
    mkdir -p /workspace && chown -R paperforge:paperforge /workspace /opt/paperforge

USER paperforge
WORKDIR /workspace

ENTRYPOINT ["paperforge"]
CMD ["preflight", "--workspace", "/workspace"]
