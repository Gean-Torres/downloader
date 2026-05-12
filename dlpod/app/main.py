import os
import uuid
import json
import subprocess
import threading
import time
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
SERVE_DIR = DOWNLOAD_DIR / "_serve"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
SERVE_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job store
jobs = {}
jobs_lock = threading.Lock()

# ── helpers ──────────────────────────────────────────────────────────────────

def detect_source(url: str) -> str:
    if "spotify.com" in url:
        return "spotify"
    return "yt"

def emit(job_id: str, line: str):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["log"].append(line)
            # Keep log size reasonable
            if len(jobs[job_id]["log"]) > 1000:
                jobs[job_id]["log"] = jobs[job_id]["log"][-1000:]

def finish(job_id: str, status: str, filename: str | None = None):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = status
            jobs[job_id]["finished_at"] = datetime.utcnow().isoformat()
            if filename:
                jobs[job_id]["filename"] = filename

def cleanup_job_dir(job_dir: Path):
    if job_dir.exists():
        try:
            shutil.rmtree(job_dir)
        except Exception as e:
            print(f"Error cleaning up {job_dir}: {e}")

# ── background tasks ─────────────────────────────────────────────────────────

def background_cleanup():
    """Periodically cleans up old files in SERVE_DIR and old jobs."""
    while True:
        time.sleep(3600)  # Run every hour
        now = datetime.utcnow()
        
        # Clean up SERVE_DIR (files older than 2 hours)
        for f in SERVE_DIR.iterdir():
            if f.is_file():
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if now - mtime > timedelta(hours=2):
                    try:
                        f.unlink()
                        print(f"Cleaned up old serve file: {f.name}")
                    except Exception as e:
                        print(f"Error deleting {f}: {e}")

        # Clean up old jobs from memory (older than 24 hours)
        with jobs_lock:
            to_delete = []
            for jid, job in jobs.items():
                if job["finished_at"]:
                    finished_at = datetime.fromisoformat(job["finished_at"])
                    if now - finished_at > timedelta(hours=24):
                        to_delete.append(jid)
            
            for jid in to_delete:
                del jobs[jid]
                print(f"Cleaned up old job from memory: {jid}")

# Start background cleanup
threading.Thread(target=background_cleanup, daemon=True).start()

# ── download workers ─────────────────────────────────────────────────────────

def run_ytdlp(job_id: str, url: str, fmt: str, quality: str, save_to_volume: bool):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Base command
    cmd = ["yt-dlp", "--no-playlist", "--newline", "--progress", "-o", str(job_dir / "%(title)s.%(ext)s")]

    # Format logic
    if fmt == "mp3":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", quality]
    elif fmt == "mp4":
        cmd += ["-f", f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]", "--merge-output-format", "mp4"]
    elif fmt == "opus":
        cmd += ["-x", "--audio-format", "opus"]
    else:
        cmd += ["-f", "bestaudio/best"]

    cmd.append(url)

    emit(job_id, f"▶ Running yt-dlp: {' '.join(cmd)}")
    
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            emit(job_id, line.rstrip())
        proc.wait()
    except Exception as e:
        emit(job_id, f"❌ Process error: {str(e)}")
        finish(job_id, "error")
        cleanup_job_dir(job_dir)
        return

    if proc.returncode != 0:
        emit(job_id, f"❌ yt-dlp exited with code {proc.returncode}")
        finish(job_id, "error")
        cleanup_job_dir(job_dir)
        return

    files = list(job_dir.iterdir())
    if not files:
        emit(job_id, "❌ No files found after download")
        finish(job_id, "error")
        cleanup_job_dir(job_dir)
        return

    # Sort files by size or name to pick the most likely candidate if multiple
    output_file = sorted(files, key=lambda f: f.stat().st_size, reverse=True)[0]
    
    if save_to_volume:
        # Move file to the root of DOWNLOAD_DIR
        final_dest = DOWNLOAD_DIR / output_file.name
        # Avoid overwriting existing files with same name
        if final_dest.exists():
            final_dest = DOWNLOAD_DIR / f"{job_id}_{output_file.name}"
        
        output_file.rename(final_dest)
        finish(job_id, "done", str(final_dest))
        emit(job_id, f"✅ Saved to volume: {final_dest.name}")
    else:
        # Move to serve area
        dest = SERVE_DIR / f"{job_id}_{output_file.name}"
        output_file.rename(dest)
        with jobs_lock:
            jobs[job_id]["serve_path"] = str(dest)
        finish(job_id, "done", str(dest))
        emit(job_id, "✅ Ready for browser download")

    cleanup_job_dir(job_dir)


def run_spotdl(job_id: str, url: str, fmt: str, save_to_volume: bool):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # spotdl command
    cmd = ["spotdl", "download", url, "--output", str(job_dir), "--format", fmt if fmt in ("mp3", "opus", "flac", "ogg") else "mp3"]

    emit(job_id, f"▶ Running spotdl: {' '.join(cmd)}")
    
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            emit(job_id, line.rstrip())
        proc.wait()
    except Exception as e:
        emit(job_id, f"❌ Process error: {str(e)}")
        finish(job_id, "error")
        cleanup_job_dir(job_dir)
        return

    files = list(job_dir.iterdir())
    if not files or proc.returncode != 0:
        emit(job_id, f"❌ spotdl failed (code {proc.returncode}) or no files produced")
        finish(job_id, "error")
        cleanup_job_dir(job_dir)
        return

    # Handle multiple files (playlists/albums) by zipping
    if len(files) > 1:
        emit(job_id, "📦 Multiple files detected, zipping...")
        import zipfile
        zip_name = f"{job_id}_playlist.zip"
        zip_path = (DOWNLOAD_DIR if save_to_volume else SERVE_DIR) / zip_name
        
        with zipfile.ZipFile(zip_path, "w") as zf:
            for f in files:
                zf.write(f, f.name)
        
        finish(job_id, "done", str(zip_path))
        if not save_to_volume:
            with jobs_lock:
                jobs[job_id]["serve_path"] = str(zip_path)
        emit(job_id, f"✅ Zip ready: {zip_name}")
    else:
        output_file = files[0]
        if save_to_volume:
            final_dest = DOWNLOAD_DIR / output_file.name
            if final_dest.exists():
                final_dest = DOWNLOAD_DIR / f"{job_id}_{output_file.name}"
            output_file.rename(final_dest)
            finish(job_id, "done", str(final_dest))
            emit(job_id, f"✅ Saved to volume: {final_dest.name}")
        else:
            dest = SERVE_DIR / f"{job_id}_{output_file.name}"
            output_file.rename(dest)
            with jobs_lock:
                jobs[job_id]["serve_path"] = str(dest)
            finish(job_id, "done", str(dest))
            emit(job_id, "✅ Ready for browser download")

    cleanup_job_dir(job_dir)


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json or {}
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")
    save_to_volume = data.get("save_to_volume", False)

    if not url:
        return jsonify({"error": "URL is required"}), 400

    job_id = str(uuid.uuid4())
    source = detect_source(url)

    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "url": url,
            "source": source,
            "format": fmt,
            "quality": quality,
            "save_to_volume": save_to_volume,
            "status": "running",
            "log": [],
            "filename": None,
            "serve_path": None,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
        }

    if source == "spotify":
        t = threading.Thread(target=run_spotdl, args=(job_id, url, fmt, save_to_volume), daemon=True)
    else:
        t = threading.Thread(target=run_ytdlp, args=(job_id, url, fmt, quality, save_to_volume), daemon=True)
    t.start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    with jobs_lock:
        # Return last 50 jobs
        return jsonify(list(reversed(list(jobs.values())))[:50])


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)


@app.route("/api/jobs/<job_id>/log", methods=["GET"])
def get_log(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"log": job["log"], "status": job["status"]})


@app.route("/api/jobs/<job_id>/download", methods=["GET"])
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404

    serve_path = job.get("serve_path")
    if not serve_path or not Path(serve_path).exists():
        return jsonify({"error": "File not found on disk"}), 404

    return send_file(serve_path, as_attachment=True)


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    with jobs_lock:
        job = jobs.pop(job_id, None)
    if not job:
        return jsonify({"error": "Not found"}), 404
    
    # Clean up serve file if present
    sp = job.get("serve_path")
    if sp and Path(sp).exists():
        try:
            Path(sp).unlink(missing_ok=True)
        except:
            pass
    return jsonify({"ok": True})


@app.route("/api/formats", methods=["GET"])
def get_formats():
    return jsonify({
        "yt": ["mp3", "mp4", "opus", "best"],
        "spotify": ["mp3", "opus", "flac", "ogg"],
        "qualities": {
            "mp3": ["128", "192", "256", "320"],
            "mp4": ["480", "720", "1080", "1440", "2160"],
        }
    })


@app.route("/api/health", methods=["GET"])
def health_check():
    # Check if tools are available
    try:
        ytdlp_version = subprocess.check_output(["yt-dlp", "--version"], text=True).strip()
    except Exception:
        ytdlp_version = "not found"
    return jsonify({
        "status": "ok",
        "ytdlp_version": ytdlp_version,
        "jobs_count": len(jobs)
    })


# ── SPA fallback ──────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
