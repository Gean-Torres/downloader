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

import re

def emit(job_id: str, line: str):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["log"].append(line)
            jobs[job_id]["last_activity"] = datetime.utcnow().isoformat()
            
            # Basic progress parsing for yt-dlp
            # Example: [download]  10.0% of 100.00MiB at 1.00MiB/s ETA 01:30
            if "[download]" in line and "%" in line:
                match = re.search(r"(\d+\.\d+)%", line)
                if match:
                    jobs[job_id]["progress"] = float(match.group(1))
            
            # Basic progress parsing for spotdl
            # Example: 10%|██        | 1/10 [00:01<00:09, 1.00it/s]
            elif "%|" in line:
                match = re.search(r"(\d+)%", line)
                if match:
                    jobs[job_id]["progress"] = float(match.group(1))

            # Keep log size reasonable
            if len(jobs[job_id]["log"]) > 1000:
                jobs[job_id]["log"] = jobs[job_id]["log"][-1000:]

def finish(job_id: str, status: str, filename: str | None = None):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = status
            jobs[job_id]["finished_at"] = datetime.utcnow().isoformat()
            jobs[job_id]["last_activity"] = datetime.utcnow().isoformat()
            jobs[job_id]["progress"] = 100 if status == "done" else jobs[job_id].get("progress", 0)
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
    """Periodically cleans up old files in SERVE_DIR and old/stuck jobs."""
    while True:
        time.sleep(60)  # Check more frequently
        now = datetime.utcnow()
        
        # Clean up SERVE_DIR (files older than 2 hours)
        try:
            for f in SERVE_DIR.iterdir():
                if f.is_file():
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if now - mtime > timedelta(hours=2):
                        try:
                            f.unlink()
                        except:
                            pass
        except:
            pass

        with jobs_lock:
            to_delete = []
            for jid, job in jobs.items():
                # Clean up finished jobs older than 24 hours
                if job["finished_at"]:
                    finished_at = datetime.fromisoformat(job["finished_at"])
                    if now - finished_at > timedelta(hours=24):
                        to_delete.append(jid)
                        continue
                
                # Mark jobs stuck in "running" as "error" if no activity for 10 mins
                if job["status"] == "running" and job.get("last_activity"):
                    last_activity = datetime.fromisoformat(job["last_activity"])
                    if now - last_activity > timedelta(minutes=10):
                        job["status"] = "error"
                        job["log"].append("❌ Job timed out (no activity for 10 minutes)")
                        job["finished_at"] = now.isoformat()
            
            for jid in to_delete:
                del jobs[jid]

# Start background cleanup
threading.Thread(target=background_cleanup, daemon=True).start()

# ── download workers ─────────────────────────────────────────────────────────

def run_ytdlp(job_id: str, url: str, fmt: str, quality: str):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    with jobs_lock:
        job_title = jobs[job_id].get("title", "download")
        jobs[job_id]["last_activity"] = datetime.utcnow().isoformat()
        jobs[job_id]["progress"] = 0

    try:
        # Base command - removed --no-playlist
        cmd = ["yt-dlp", "--newline", "--progress", "-o", str(job_dir / "%(title)s.%(ext)s")]

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
            return

        if proc.returncode != 0:
            emit(job_id, f"❌ yt-dlp exited with code {proc.returncode}")
            finish(job_id, "error")
            return

        files = list(job_dir.iterdir())
        if not files:
            emit(job_id, "❌ No files found after download")
            finish(job_id, "error")
            return

        if len(files) > 1:
            # Playlist/Album: Move folder to DOWNLOAD_DIR
            final_dest_dir = DOWNLOAD_DIR / job_title
            # Avoid overwriting or mixing with existing folders
            if final_dest_dir.exists():
                final_dest_dir = DOWNLOAD_DIR / f"{job_title}_{job_id[:8]}"
            
            shutil.move(str(job_dir), str(final_dest_dir))
            
            # Create zip for web download
            import zipfile
            zip_name = f"{final_dest_dir.name}.zip"
            zip_path = SERVE_DIR / zip_name
            with zipfile.ZipFile(zip_path, "w") as zf:
                for f in final_dest_dir.rglob("*"):
                    zf.write(f, f.relative_to(final_dest_dir))
            
            with jobs_lock:
                jobs[job_id]["serve_path"] = str(zip_path)
            finish(job_id, "done", str(final_dest_dir))
            emit(job_id, f"✅ Saved playlist to volume: {final_dest_dir.name}")
            emit(job_id, "✅ Zip ready for browser download")
        else:
            # Single file
            output_file = files[0]
            final_dest = DOWNLOAD_DIR / output_file.name
            if final_dest.exists():
                final_dest = DOWNLOAD_DIR / f"{job_id[:8]}_{output_file.name}"
            
            output_file.rename(final_dest)
            
            # Move copy to serve area for web download
            serve_copy = SERVE_DIR / final_dest.name
            shutil.copy2(final_dest, serve_copy)
            
            with jobs_lock:
                jobs[job_id]["serve_path"] = str(serve_copy)
            finish(job_id, "done", str(final_dest))
            emit(job_id, f"✅ Saved to volume: {final_dest.name}")
            emit(job_id, "✅ Ready for browser download")

    except Exception as e:
        emit(job_id, f"❌ Unexpected error: {str(e)}")
        finish(job_id, "error")
    finally:
        cleanup_job_dir(job_dir)


def run_spotdl(job_id: str, url: str, fmt: str):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    with jobs_lock:
        job_title = jobs[job_id].get("title", "download")
        jobs[job_id]["last_activity"] = datetime.utcnow().isoformat()
        jobs[job_id]["progress"] = 0

    try:
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
            return

        files = list(job_dir.iterdir())
        if not files or proc.returncode != 0:
            emit(job_id, f"❌ spotdl failed (code {proc.returncode}) or no files produced")
            finish(job_id, "error")
            return

        if len(files) > 1:
            # Playlist/Album: Move folder to DOWNLOAD_DIR
            final_dest_dir = DOWNLOAD_DIR / job_title
            if final_dest_dir.exists():
                final_dest_dir = DOWNLOAD_DIR / f"{job_title}_{job_id[:8]}"
            
            shutil.move(str(job_dir), str(final_dest_dir))
            
            # Create zip for web download
            import zipfile
            zip_name = f"{final_dest_dir.name}.zip"
            zip_path = SERVE_DIR / zip_name
            with zipfile.ZipFile(zip_path, "w") as zf:
                for f in final_dest_dir.rglob("*"):
                    zf.write(f, f.relative_to(final_dest_dir))
            
            with jobs_lock:
                jobs[job_id]["serve_path"] = str(zip_path)
            finish(job_id, "done", str(final_dest_dir))
            emit(job_id, f"✅ Saved playlist to volume: {final_dest_dir.name}")
            emit(job_id, "✅ Zip ready for browser download")
        else:
            # Single file
            output_file = files[0]
            final_dest = DOWNLOAD_DIR / output_file.name
            if final_dest.exists():
                final_dest = DOWNLOAD_DIR / f"{job_id[:8]}_{output_file.name}"
            
            output_file.rename(final_dest)
            
            # Move copy to serve area for web download
            serve_copy = SERVE_DIR / final_dest.name
            shutil.copy2(final_dest, serve_copy)
            
            with jobs_lock:
                jobs[job_id]["serve_path"] = str(serve_copy)
            finish(job_id, "done", str(final_dest))
            emit(job_id, f"✅ Saved to volume: {final_dest.name}")
            emit(job_id, "✅ Ready for browser download")

    except Exception as e:
        emit(job_id, f"❌ Unexpected error: {str(e)}")
        finish(job_id, "error")
    finally:
        cleanup_job_dir(job_dir)


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    source = detect_source(url)
    try:
        # For yt-dlp sources (YouTube, etc)
        if source == "yt":
            cmd = ["yt-dlp", "--dump-json", "--flat-playlist", "--no-warnings", url]
            output = subprocess.check_output(cmd, text=True)
            info = json.loads(output)
            title = info.get("title") or info.get("playlist_title") or "Unknown Title"
            return jsonify({"title": title})
        else:
            # Fallback for Spotify if yt-dlp info fails or isn't detailed
            # In a real app we might use spotdl --search or similar, but for now:
            return jsonify({"title": "Spotify Media"})
    except Exception as e:
        return jsonify({"title": "Unknown Media", "error": str(e)}), 200

@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json or {}
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")
    title = data.get("title", "download").strip()

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
            "title": title,
            "status": "running",
            "log": [],
            "filename": None,
            "serve_path": None,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
        }

    if source == "spotify":
        t = threading.Thread(target=run_spotdl, args=(job_id, url, fmt), daemon=True)
    else:
        t = threading.Thread(target=run_ytdlp, args=(job_id, url, fmt, quality), daemon=True)
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
