# Minimal research sandbox image for safety_auto_research agent-mode tasks.
#
# Built ONCE (lazily) by scripts/build_agent_sandbox.sh. The research repo is
# mounted read-only at runtime, so this image only needs the heavy scientific
# dependencies preinstalled — pure-compute research code runs fully offline
# (--network none) once built.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Build toolchain + CA certs (needed to install wheels; network is available at
# IMAGE BUILD time only — runtime containers run with --network none).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Preinstall the common research deps so per-run containers start instantly and
# never need network. Add more here as new agent-mode tasks require them.
RUN pip install --no-cache-dir \
    scikit-learn \
    pandas \
    numpy \
    scipy

WORKDIR /repo
CMD ["/bin/bash"]
