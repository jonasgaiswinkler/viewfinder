# ---- Build & runtime image for ViewFinder backend ----
# Based on Debian bookworm with GDAL, osm2pgrouting, and Python 3.12.
FROM python:3.12-slim-bookworm AS base

# Prevent interactive prompts during package install
ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# System dependencies: GDAL, osm2pgrouting, osmium, and build tools
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        # GDAL runtime + headers (needed by rasterio / osgeo Python bindings)
        gdal-bin \
        libgdal-dev \
        # osm2pgrouting and its runtime deps (pulls in libpgrouting, boost, …)
        osm2pgrouting \
        # osmium-tool for local OSM data extraction and filtering
        osmium-tool \
        # Build tools needed to compile some Python wheels
        gcc \
        g++ \
        # Useful at runtime for health-checks / debugging
        curl \
    && rm -rf /var/lib/apt/lists/*

# Expose the GDAL version so Python wheels can find headers
ENV GDAL_CONFIG=/usr/bin/gdal-config

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .

# Install Python packages.  GDAL wheel needs numpy headers first.
RUN pip install --no-cache-dir numpy && \
    pip install --no-cache-dir GDAL=="$(gdal-config --version)" && \
    pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Application code & config
# ---------------------------------------------------------------------------
COPY src/ ./src/
COPY config/ ./config/

# ---------------------------------------------------------------------------
# Temp-file volume
# ---------------------------------------------------------------------------
# OSM downloads (.osm) and DEM tiles (.tif) are written to the system temp
# directory.  Mounting a volume here keeps the container layer thin and makes
# it easy to inspect / persist artefacts between runs.
VOLUME ["/tmp"]

# ---------------------------------------------------------------------------
# OSM Cache volume
# ---------------------------------------------------------------------------
# Pre-filtered OSM data for fast local bounding-box queries.
# Mount a persistent volume here to avoid re-downloading on container restart.
VOLUME ["/data/osm"]

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
EXPOSE 8000

ENV LOG_LEVEL=info

# Run uvicorn from the src directory so relative imports resolve correctly
WORKDIR /app/src

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000 --log-level $LOG_LEVEL"]
