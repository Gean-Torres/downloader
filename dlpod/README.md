# DLPOD — yt-dlp + spotdl Web Wrapper

A minimal web UI to download media via `yt-dlp` (YouTube, etc.) and `spotdl` (Spotify),
deployable as a Podman pod with systemd.

## Structure

```
dlpod/
├── app/
│   ├── main.py            # Flask backend
│   ├── requirements.txt
│   ├── Containerfile      # Podman/Docker image definition
│   └── static/
│       └── index.html     # Single-page frontend
├── dlpod-pod.yaml         # Podman kube YAML
└── dlpod.service          # systemd unit
```

## Build & Deploy

### 1. Build the image

```bash
cd dlpod/app
podman build -t dlpod:latest -f Containerfile .
```

### 2. Prepare the download directory

```bash
# Adjust this path to wherever you want files saved
sudo mkdir -p /srv/Downloads/media
sudo chown $USER:$USER /srv/Downloads/media
```

Edit `dlpod-pod.yaml` if needed, but it currently defaults to:
```yaml
hostPort: 8765          # port exposed on the host
path: /srv/Downloads/media  # host path for saved files
```

### 3. Install files

```bash
sudo mkdir -p /opt/dlpod
sudo cp dlpod-pod.yaml /opt/dlpod/
sudo cp dlpod.service /etc/systemd/system/
```

### 4. Enable & start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dlpod
```

### 5. Access

Open `http://<server-ip>:8765` in your browser.

---

## Rootless Podman (user session)

If you prefer rootless:

```bash
mkdir -p ~/.config/systemd/user
cp dlpod.service ~/.config/systemd/user/
# Edit the unit: change ExecStart paths to use your home dir
systemctl --user daemon-reload
systemctl --user enable --now dlpod
loginctl enable-linger $USER   # keep running after logout
```

---

## Nginx reverse proxy (optional)

Add to your existing Nginx config:

```nginx
location /dlpod/ {
    proxy_pass         http://127.0.0.1:8765/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_buffering    off;
    proxy_read_timeout 3600s;   # long timeout for big downloads
}
```

---

## Environment variables

| Variable       | Default      | Description                        |
|----------------|--------------|------------------------------------|
| `DOWNLOAD_DIR` | `/downloads` | Where files are stored in container |

---

## Notes

- Files saved to volume land in `DOWNLOAD_DIR` (mapped to your host path).
- Files NOT saved to volume are served directly to the browser and stored
  temporarily under `DOWNLOAD_DIR/_serve/` — they are NOT auto-cleaned yet;
  add a cron `find /srv/dlpod/downloads/_serve -mtime +1 -delete` if needed.
- Spotify downloads require a valid internet connection and use YouTube Music
  as the audio source (no Spotify credentials needed).
- Update yt-dlp regularly — it breaks often as sites change:
  `podman exec dlpod-dlpod-app pip install -U yt-dlp`
