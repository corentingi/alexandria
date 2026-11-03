# Running Alexandria on a NAS

Alexandria is one container and one data folder. These steps use the image published on
GitHub's registry, built for both Intel/AMD (most Synology "+" and QNAP models) and ARM.

## 1. The data folder

Create it on the NAS's own disks (a shared folder such as `docker` is fine), **not** on a
network mount: SQLite needs working file locks.

The app runs as uid 1000 inside the container. Give it the folder:

```sh
sudo mkdir -p /volume1/docker/alexandria
sudo chown -R 1000:1000 /volume1/docker/alexandria
```

On Synology, run these over SSH (Control Panel → Terminal & SNMP → enable SSH), or create
the folder in File Station and set the owner with the command above.

## 2. Start it

Copy [`compose.nas.yml`](compose.nas.yml) to the NAS as `docker-compose.yml`, adjust the
volume path, then:

```sh
docker compose up -d
docker compose logs | grep -A2 "shown once"   # the first admin token
```

In Synology Container Manager: **Project → Create**, pick the folder, paste the file.

Open `http://<nas>:8080`, paste the token, and create more tokens (one per agent or
device) under **Settings → Tokens**.

## 3. Update

```sh
docker compose pull && docker compose up -d
```

`:latest` follows the main branch; pin a version (`:0.1.0`) to update only when you choose.
Data stays in the folder. If release notes ask for it, rebuild the search index under
**Settings → Index** (it is rebuilt from the files).

## HTTPS (optional)

Put a reverse proxy in front (Synology: Control Panel → Login Portal → Advanced → Reverse
Proxy) and tell Alexandria about it, so links and cookies use https:

```yaml
ALEXANDRIA_HTTP__PUBLIC_URL: "https://library.example.lan"
ALEXANDRIA_HTTP__FORWARDED_ALLOW_IPS: "*"   # or the proxy's IP
```

## Remote access

Do not forward port 8080 to the internet. Use a VPN (Tailscale, WireGuard, the NAS's VPN
server) to reach it from outside. See [Security](../README.md#security).

## Back up

The data folder is the whole state: snapshot it with the NAS (Snapshot Replication,
Hyper Backup) or use **Settings → Export & import** for portable `.tar` parts.
