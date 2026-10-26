# Small helper image with osmium-tool (https://osmcode.org/osmium-tool/, GPL-3) used by
# docker/osrm/setup.sh and setup.ps1 to filter, merge and clip the Geofabrik extracts
# before OSRM processes them.  Built locally as "routeopt-osmium"; nothing is published.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends osmium-tool ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY prepare.sh /usr/local/bin/routeopt-prepare
# Strip Windows line endings in case the checkout converted them, then make it executable.
RUN sed -i 's/\r$//' /usr/local/bin/routeopt-prepare && chmod +x /usr/local/bin/routeopt-prepare

WORKDIR /data
CMD ["osmium", "--version"]
