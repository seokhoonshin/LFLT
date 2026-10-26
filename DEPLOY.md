# DEPLOY.md — setting RouteOpt up on another machine from the source zip

The zip (`routeopt-src-<date>.zip`, under 1 MB) holds the application source, the docs,
the sample data and the OSRM build scripts — **not** the map data.  On the new machine
`docker/osrm/setup.ps1` (Windows) or `docker/osrm/setup.sh` (Linux/macOS) downloads the
Geofabrik extracts (≈ 1.7 GB) and builds the routing dataset; measured on a 16 GB laptop
that takes about 17 minutes and leaves ≈ 4.6 GB in `docker/osrm/data`.

## What the machine needs

| | |
|---|---|
| Docker | Docker Desktop (Windows/macOS) or Docker Engine (Linux) with the `docker compose` plugin, running |
| Python | 3.12 or newer (Ubuntu 24.04 ships 3.12; on 22.04 install `python3.12 python3.12-venv` from the deadsnakes PPA, or use [uv](https://docs.astral.sh/uv/)) |
| RAM | 8 GB is enough; the build stayed inside a 10 GB Docker limit on a 16 GB laptop, the server needs ≈ 2–3 GB, the app and solver ≈ 2 GB |
| Disk | ≈ 10 GB free (downloads + routing data + packages) |
| Network out | Geofabrik (map downloads), Docker Hub / GitHub Container Registry (images), Debian mirrors (the osmium helper image), PyPI (packages), the US Census geocoder (only for upload rows without coordinates), OpenStreetMap tile servers (map background in the browser) |

## Windows (PowerShell)

```powershell
Expand-Archive routeopt-src-*.zip -DestinationPath .
cd routeopt
cd docker\osrm
.\setup.ps1                 # downloads + builds the map, ≈ 20 min; -IncludeMexico for northern Baja
cd ..\..
.\start.ps1                 # starts the OSRM container, creates .venv + installs on first run, launches the app
```

Then open http://localhost:8501.  If PowerShell refuses to run the scripts:
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

## Linux (Ubuntu/Debian VM)

```bash
unzip routeopt-src-*.zip
cd routeopt
bash docker/osrm/setup.sh   # INCLUDE_MEXICO=1 bash docker/osrm/setup.sh for northern Baja
bash start.sh               # OSRM container + .venv + app on http://<server-ip>:8501
```

`start.sh` / `start.ps1` run `docker compose up -d` in `docker/osrm`, create `.venv` and
`pip install -e .` on the first run, print `routeopt osrm-check`, and launch Streamlit in
headless mode (`ROUTEOPT_HOST` / `ROUTEOPT_PORT` override the default 0.0.0.0:8501).
Ctrl-C stops the app; the OSRM container keeps running and restarts with Docker.  If
`docker` needs `sudo` on the VM, add your user to the docker group
(`sudo usermod -aG docker $USER`, log out and in) or run the scripts with `sudo -E`.

The same steps by hand:

```bash
cd docker/osrm && docker compose up -d && cd ../..
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -e .
routeopt osrm-check                                   # -> "OSRM at http://localhost:5000: healthy"
streamlit run app/Home.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
```

## Keep it running after you log out (Linux)

Quick way: `nohup bash start.sh > routeopt.log 2>&1 &`.  Proper way — a systemd unit
(`/etc/systemd/system/routeopt.service`):

```ini
[Unit]
Description=RouteOpt (Streamlit)
After=docker.service network-online.target
Wants=docker.service

[Service]
User=ubuntu                      # the account that owns the unzipped folder
WorkingDirectory=/home/ubuntu/routeopt
ExecStart=/usr/bin/bash /home/ubuntu/routeopt/start.sh
Restart=on-failure
Environment=ROUTEOPT_HOST=0.0.0.0
Environment=ROUTEOPT_PORT=8501

[Install]
WantedBy=multi-user.target
```

`sudo systemctl daemon-reload && sudo systemctl enable --now routeopt`.

## Security

* Streamlit has **no login**.  Keep port 8501 behind the cloud firewall / security group,
  a VPN, or an SSH tunnel (`ssh -L 8501:localhost:8501 user@server`, then open
  http://localhost:8501), or put nginx with basic auth in front of it.
* Do not expose OSRM (port 5000) publicly: in `docker/osrm/docker-compose.yml` change
  `"5000:5000"` to `"127.0.0.1:5000:5000"` and run `docker compose up -d` again.
* Every upload, run, version, edit and export is stored under `data/` (SQLite
  `data/routeopt.db`, `data/uploads`, `data/exports`) — back that folder up, or point
  `ROUTEOPT_DB_URL` in `.env` at Postgres/SQL Server (see `.env.example`).

## Settings and upkeep

* Copy `.env.example` to `.env` and edit if needed; the defaults (`OSRM_URL=http://localhost:5000`,
  SQLite in `data/`) work out of the box.
* The map is a snapshot of OpenStreetMap from the day `setup` ran.  To refresh it later:
  `docker compose down`, `.\setup.ps1 -ForceDownload` (or `FORCE_DOWNLOAD=1 ./setup.sh`),
  `docker compose up -d`, then clear the travel-time cache (History page → "Clear travel
  cache", or `routeopt clear-cache`).  Changing the radius or the hub: `docker/osrm/README.md`.
* Moving an already-built dataset instead of rebuilding: copy
  `docker/osrm/data/routeopt-coverage.osrm.*` (≈ 2.7 GB) into the same folder on the new
  machine and skip the `setup` step.
