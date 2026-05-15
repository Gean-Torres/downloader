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
├── package.json           # Node.js scripts for development
├── dlpod-pod.yaml         # Podman kube YAML
└── dlpod.service          # systemd unit
```

## Local Development (Easiest for Live Testing)

If you have Node.js and Python 3 installed, you can use the following workflow:

### 1. Initial Setup
```bash
npm install
npm run setup
```
This installs `nodemon` for auto-reloading and sets up a Python virtual environment with all dependencies.

### 2. Run in Development Mode
```bash
npm run dev
```
This will start the Flask server on `http://127.0.0.1:5000` and watch for any changes in `app/main.py` or `app/static/index.html`, automatically restarting the server when you save.

### 3. Run Tests
```bash
npm test
```

## Build & Deploy (Production)

### 1. Build & Prepare Configuration

The `deploy.sh` script builds the image and prepares the Kubernetes YAML in your home directory (`~/.config/dlpod/dlpod-pod.yaml`).

```bash
./deploy.sh
```

### 2. Deploy via Cockpit Podman (Recommended)

1. Open Cockpit in your browser (usually `https://<server-ip>:9090`).
2. Navigate to the **Podman containers** (or **Containers**) section.
3. Click on the dropdown/button to **Create pod** or **Play Kubernetes YAML**.
4. Point it to the generated file: `~/.config/dlpod/dlpod-pod.yaml`.
5. Cockpit will create the pod and start the containers.

### 3. Deploy via systemd (Optional)

If you still prefer systemd management:

```bash
./deploy.sh --systemd
systemctl --user enable --now dlpod
```

### 4. Access

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
