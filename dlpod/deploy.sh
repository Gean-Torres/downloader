#!/bin/bash
# deploy.sh - Rootless deployment script for DLPOD
set -e

APP_NAME="dlpod"
CONFIG_DIR="$HOME/.config/$APP_NAME"
SERVICE_DIR="$HOME/.config/systemd/user"
DOWNLOAD_DIR="$HOME/Downloads/dlpod"
PODMAN_BIN=$(command -v podman)

if [ -z "$PODMAN_BIN" ]; then
    echo "Error: podman not found. Please install podman."
    exit 1
fi

echo "--- Building Podman image ---"
$PODMAN_BIN build -t dlpod:latest -f app/Containerfile app/

echo "--- Preparing directories ---"
mkdir -p "$CONFIG_DIR"
mkdir -p "$SERVICE_DIR"
mkdir -p "$DOWNLOAD_DIR"

echo "--- Patching and installing configuration ---"
# 1. Patch dlpod-pod.yaml: Set host download path to user's Downloads folder
sed "s|path: /srv/Downloads/media|path: $DOWNLOAD_DIR|g" dlpod-pod.yaml > "$CONFIG_DIR/dlpod-pod.yaml"

# 2. Patch dlpod.service: 
#    - Update paths from /opt/dlpod to the user's config dir
#    - Update podman path to the one found on this system
#    - Change WantedBy from multi-user.target to default.target (required for --user services)
sed -e "s|/opt/dlpod/dlpod-pod.yaml|$CONFIG_DIR/dlpod-pod.yaml|g" \
    -e "s|/usr/bin/podman|$PODMAN_BIN|g" \
    -e "s|multi-user.target|default.target|g" \
    dlpod.service > "$SERVICE_DIR/dlpod.service"

echo "--- Starting systemd service ---"
systemctl --user daemon-reload
systemctl --user enable --now dlpod

echo ""
echo "===================================================="
echo " Deployment Complete!"
echo "===================================================="
echo " Web UI:      http://localhost:8765"
echo " Downloads:   $DOWNLOAD_DIR"
echo " Service:     systemctl --user status dlpod"
echo " Logs:        journalctl --user -u dlpod -f"
echo "===================================================="

# Show status
systemctl --user status dlpod --no-pager || true
