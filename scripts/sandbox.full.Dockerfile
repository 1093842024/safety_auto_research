# Full research sandbox image for safety_auto_research agent-mode tasks.
#
# Extends the minimal sandbox.Dockerfile with the heavy deps required by:
#   * custom.* cls tasks   -> torch / torchvision / librosa
#   * SAB 19 agent-eval     -> neurokit2, biopsykit, ccobra, geopandas, rdkit,
#                              MDAnalysis, prolif, mastml, matminer, cftime, iris
#
# Built ONCE (lazily / manually). Runtime containers still run with --network none,
# so adding preinstalled libs does NOT violate the offline-isolation contract.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UDUNITS2_XML_PATH=/usr/share/xml/udunits/udunits2.xml

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
        ffmpeg \
        libudunits2-0 libudunits2-dev \
        libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

# Base scientific deps (mirror sandbox.Dockerfile).
# IMPORTANT: pin numpy to 1.26.x. rdkit-pypi / MDAnalysis prebuilt wheels are
# compiled against the NumPy 1.x C ABI; on NumPy 2.x they fail at import/runtime
# with "_ARRAY_API not found". 1.26.4 is compatible with torch, sklearn, pandas,
# scipy, iris, etc.
RUN pip install --no-cache-dir \
    scikit-learn \
    pandas \
    "numpy==1.26.4" \
    scipy

# Torch (CPU-only to keep image lean). Default PyPI ships the CPU wheel for
# Linux, so no special index URL is required (the pytorch cpu index was
# unreachable from this build host).
RUN pip install --no-cache-dir \
    torch torchvision

# Audio + SAB domain libs (default PyPI index).
# NOTE: ts2vg has NO aarch64/linux wheel on PyPI (only x86_64-linux + macOS-arm64)
# and its published sdist is missing the Cython .pyx/.pxd sources, so it cannot be
# built normally. We supply a locally-built aarch64 wheel (patched: fetched the
# missing _base.pxd + graph/*.pyx from the v1.2.4 tag and compiled with Cython
# 0.29.37). Install it FIRST so `pip install biopsykit` sees ts2vg satisfied and
# skips its broken sdist build.
COPY ts2vg-1.2.4-cp311-cp311-linux_aarch64.whl /tmp/ts2vg.whl
RUN pip install --no-cache-dir /tmp/ts2vg.whl \
    && pip install --no-cache-dir \
    librosa \
    neurokit2 \
    biopsykit \
    ccobra \
    geopandas \
    rdkit-pypi \
    MDAnalysis \
    prolif \
    matminer \
    cftime \
    scitools-iris \
    mastml

WORKDIR /repo
CMD ["/bin/bash"]
