FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build pkg-config \
      libgrpc++-dev libprotobuf-dev protobuf-compiler protobuf-compiler-grpc \
      libgtest-dev \
      python3 python3-venv \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY python/requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements-dev.txt

RUN mkdir -p /work/build && chown ubuntu:ubuntu /work/build
USER ubuntu
WORKDIR /work
