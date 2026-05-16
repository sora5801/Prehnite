# Base sandbox image for Prehnite v0.
#
# Goal: a small but useful Linux environment that can host a coding-agent task.
# Python + git + standard build tools, nothing exotic. The image is meant to be
# rebuilt locally; it is not pushed to a registry yet.
#
# Build:
#     docker build -t prehnite-base:latest -f docker/base.Dockerfile docker/
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        less \
        make \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
        tree \
        vim-tiny \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/local/bin/python

# Non-root user — agents inside the sandbox shouldn't run as root unless a
# task explicitly needs it.
RUN useradd --create-home --shell /bin/bash agent
WORKDIR /workspace
RUN chown agent:agent /workspace
USER agent
