FROM pytorch/pytorch:2.2.2-cuda12.1-cudnn8-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPERFORGE_ALLOW_SYSTEM_PYTHON=1

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates texlive-latex-base texlive-latex-extra \
    texlive-bibtex-extra biber chktex && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /workspace/requirements.txt
RUN python -m pip install -U pip setuptools wheel && \
    python -m pip install -r /workspace/requirements.txt

COPY . /workspace

CMD ["python", "launch_scientist.py", "--experiment", "paper_writer", "--num-ideas", "1", "--skip-novelty-check"]
