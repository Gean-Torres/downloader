import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
import shlex
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory, Response
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SERVE_DIR = DOWNLOAD_DIR / "_serve"
WORK_DIR = DOWNLOAD_DIR / "_work"
DB_PATH = DATA_DIR / "dlpod.db"

for directory in (DOWNLOAD_DIR, SERVE_DIR, WORK_DIR, DATA_DIR):
    directory.mkdir(parents=True, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()

MEDIA_EXTENSIONS = {
    ".mp3", ".mp4", ".m4a", ".webm", ".mkv", ".opus", ".ogg", ".flac", ".wav", ".aac", ".mov"
}


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                client_id TEXT,
                data TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                url TEXT,
                client_id TEXT,
                title TEXT,
                filename TEXT,
                format TEXT,
                path TEXT PRIMARY KEY,
                created_at TEXT
            )
        """)
        
        # Migration: add client_id to jobs if missing
        cursor = conn.execute("PRAGMA table_info(jobs)")
        columns = [row[1] for row in cursor.fetchall()]
        if "client_id" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN client_id TEXT")

        # Migration: add client_id to downloads if missing
        cursor = conn.execute("PRAGMA table_info(downloads)")
        columns = [row[1] for row in cursor.fetchall()]
        if "client_id" not in columns:
            conn.execute("ALTER TABLE downloads ADD COLUMN client_id TEXT")

    # Load recent jobs into memory
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT client_id, data FROM jobs ORDER BY updated_at ASC")
        for row in cursor:
            client_id, data_json = row
            job = json.loads(data_json)
            job["client_id"] = client_id
            if job.get("status") == "running":
                job["status"] = "error"
                job["log"].append("❌ Job interrupted by system restart")
                job["finished_at"] = job.get("last_activity") or utc_now()
            jobs[job["id"]] = job

    # Initial sync with filesystem
    try:
        sync_downloads_db()
    except Exception:
        pass


init_db()


def save_job_to_db(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        # Don't save the process object or client_id (stored in separate column)
        data = {k: v for k, v in job.items() if k not in ("proc", "client_id")}
        client_id = job.get("client_id")
        updated_at = data.get("last_activity") or utc_now()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, client_id, data, updated_at) VALUES (?, ?, ?, ?)",
            (job_id, client_id, json.dumps(data), updated_at)
        )


def log_admin_event(client_id: str | None, filename: str):
    log_path = DATA_DIR / "downloads.log"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ip = request.remote_addr if request else "unknown"
    client_short = (client_id[:8] if client_id else "unknown")
    
    log_line = f"[{timestamp}] IP: {ip} | User: {client_short} | File: {filename}\n"
    try:
        with open(log_path, "a") as f:
            f.write(log_line)
    except Exception as e:
        print(f"Error writing admin log: {e}")


def record_download(url: str, client_id: str | None, title: str, filename: str, fmt: str, path: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO downloads (url, client_id, title, filename, format, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, client_id, title, filename, fmt, path, utc_now())
        )
    log_admin_event(client_id, filename)


def sync_downloads_db():
    files = visible_download_files()
    file_paths = {str(p) for p in files}

    with sqlite3.connect(DB_PATH) as conn:
        # 1. Remove entries for files that no longer exist
        cursor = conn.execute("SELECT path FROM downloads")
        db_paths = {row[0] for row in cursor}
        deleted_paths = db_paths - file_paths
        if deleted_paths:
            conn.executemany("DELETE FROM downloads WHERE path = ?", [(p,) for p in deleted_paths])

        # 2. Add files that are not in the DB
        untracked_paths = file_paths - db_paths
        for path_str in untracked_paths:
            path = Path(path_str)
            # Try to infer some info from filename
            title = path.stem
            fmt = path.suffix.lstrip(".") if path.is_file() else "folder"
            conn.execute(
                "INSERT INTO downloads (url, title, filename, format, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("", title, path.name, fmt, path_str, utc_now())
            )


def utc_now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def detect_source(url: str) -> str:
    return "spotify" if "spotify.com" in url.lower() else "yt"


def infer_mode(url: str) -> str:
    lowered = url.lower()
    if any(token in lowered for token in ("list=", "/playlist", "/album", "/show", "/artist")):
        return "playlist"
    return "single"


def safe_name(value: str, fallback: str = "download") -> str:
    value = (value or fallback).strip()
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or fallback




def generated_download_title(source: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return safe_name(f"{source}-{timestamp}")


def fetch_page_title(url: str, timeout: int = 8) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type:
                return None
            raw = response.read(200000).decode("utf-8", errors="ignore")
        match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        return safe_name(title) if title else None
    except Exception:
        return None


def resolve_job_title(url: str, source: str, preferred: str | None = None) -> str:
    if preferred:
        cleaned = safe_name(preferred, fallback="").strip()
        if cleaned:
            return cleaned

    page_title = fetch_page_title(url)
    if page_title:
        return page_title

    host = (urlparse(url).hostname or source or "download").split(":")[0]
    return generated_download_title(host)

def is_media_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS


def visible_download_files() -> list[Path]:
    files = []
    for path in DOWNLOAD_DIR.iterdir():
        if path.name.startswith("_"):
            continue
        if path.is_file() or path.is_dir():
            files.append(path)
    return files


def find_duplicates(title: str, fmt: str | None = None) -> list[dict]:
    target = safe_name(title).lower()
    matches = []
    if not target:
        return matches

    # Check database for historical downloads first
    with sqlite3.connect(DB_PATH) as conn:
        query = "SELECT path FROM downloads WHERE (LOWER(title) LIKE ? OR LOWER(filename) LIKE ?)"
        params = [f"%{target}%", f"%{target}%"]
        if fmt:
            query += " AND format = ?"
            params.append(fmt)

        cursor = conn.execute(query, params)
        for row in cursor:
            path = Path(row[0])
            if path.exists():
                matches.append(artifact_response(path, cached=True))

    # Also check filesystem for untracked files
    for path in visible_download_files():
        if any(str(path) == m["path"] for m in matches):
            continue
        stem = path.stem.lower() if path.is_file() else path.name.lower()
        same_format = not fmt or path.is_dir() or path.suffix.lower().lstrip(".") == fmt.lower()
        if same_format and (stem == target or stem.startswith(f"{target}_") or target in stem):
            matches.append(artifact_response(path, cached=True))
    return matches


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.with_suffix("") if path.is_file() else path
    suffix = path.suffix if path.is_file() else ""
    i = 2
    while True:
        candidate = Path(f"{base} ({i}){suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def artifact_response(path: Path, cached: bool = False) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size": stat.st_size if path.is_file() else sum(f.stat().st_size for f in path.rglob("*") if f.is_file()),
        "cached": cached,
        "is_archive": path.suffix.lower() == ".zip",
    }


def serializable_job(job: dict) -> dict:
    clean = {k: v for k, v in job.items() if k != "proc"}
    clean["download_url"] = f"/api/jobs/{job['id']}/download" if job.get("serve_path") else None
    return clean


def emit(job_id: str, line: str):
    if not line:
        return
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["log"].append(line)
        job["last_activity"] = utc_now()

        download_match = re.search(r"\[download\]\s+([0-9]+(?:\.[0-9]+)?)%", line)
        spotdl_match = re.search(r"([0-9]{1,3})%\|", line)
        if download_match:
            job["progress"] = min(float(download_match.group(1)), 99.0)
        elif spotdl_match:
            job["progress"] = min(float(spotdl_match.group(1)), 99.0)

        if len(job["log"]) > 1000:
            job["log"] = job["log"][-1000:]


def finish(job_id: str, status: str, **updates):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["status"] = status
        job["finished_at"] = utc_now()
        job["last_activity"] = utc_now()
        if status == "done":
            job["progress"] = 100
        job.pop("proc", None)
    save_job_to_db(job_id)


def run_process(job_id: str, cmd: list[str]) -> int:
    emit(job_id, f"▶ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["proc"] = proc
    assert proc.stdout is not None
    for line in proc.stdout:
        emit(job_id, line.rstrip())
    proc.wait()
    return proc.returncode


def cleanup_job_dir(job_dir: Path):
    try:
        if job_dir.exists():
            shutil.rmtree(job_dir)
    except Exception as exc:
        print(f"Error cleaning up {job_dir}: {exc}")


def stream_folder_as_zip(folder_path: Path):
    import io
    import zipfile

    def generator():
        # We use a memory buffer to store chunks of the ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
            for path in sorted(folder_path.rglob("*")):
                if not path.is_file():
                    continue
                zf.write(path, path.relative_to(folder_path))
                # Yield what we have so far
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        # Finalize and yield remaining
        yield buf.getvalue()

    return generator()


def register_single_artifact(job_id: str, source_file: Path, duplicate_action: str, partial: bool = False) -> None:
    final_dest = DOWNLOAD_DIR / source_file.name
    if final_dest.exists():
        if duplicate_action == "override":
            final_dest.unlink()
        elif duplicate_action == "reuse":
            # Serve directly from the existing permanent storage
            finish(job_id, "done", filename=str(final_dest), serve_path=str(final_dest), artifacts=[artifact_response(final_dest, cached=True)], duplicate_used=True)
            emit(job_id, f"♻ Reused cached file: {final_dest.name}")
            return
        else:
            final_dest = unique_path(final_dest)

    shutil.move(str(source_file), str(final_dest))
    
    # Record download to DB
    with jobs_lock:
        job = jobs.get(job_id, {})
        url, client_id, title, fmt = job.get("url", ""), job.get("client_id"), job.get("title", ""), job.get("format", "")
    record_download(url, client_id, title, final_dest.name, fmt, str(final_dest))

    # Serve directly from the permanent storage
    finish(job_id, "done", filename=str(final_dest), serve_path=str(final_dest), artifacts=[artifact_response(final_dest)], partial=partial)
    emit(job_id, f"✅ Ready for browser download: {final_dest.name}")


def register_playlist_artifact(job_id: str, job_dir: Path, title: str, duplicate_action: str, partial: bool = False) -> None:
    final_dir = DOWNLOAD_DIR / safe_name(title)
    if final_dir.exists():
        if duplicate_action == "override":
            shutil.rmtree(final_dir)
        elif duplicate_action == "reuse":
            finish(job_id, "done", filename=str(final_dir), serve_path=None, artifacts=[artifact_response(final_dir, cached=True)], duplicate_used=True, is_playlist=True)
            emit(job_id, f"♻ Reused cached playlist folder: {final_dir.name}")
            return
        else:
            final_dir = unique_path(final_dir)

    shutil.move(str(job_dir), str(final_dir))

    with jobs_lock:
        job = jobs.get(job_id, {})
        url, client_id, job_title, fmt = job.get("url", ""), job.get("client_id"), job.get("title", ""), job.get("format", "")
    record_download(url, client_id, job_title or title, final_dir.name, fmt, str(final_dir))

    finish(job_id, "done", filename=str(final_dir), serve_path=None, artifacts=[artifact_response(final_dir)], is_playlist=True, partial=partial)
    emit(job_id, f"✅ Playlist folder saved: {final_dir.name}")


def finalize_outputs(job_id: str, job_dir: Path, title: str, mode: str, duplicate_action: str, partial: bool = False) -> None:
    media_files = [p for p in job_dir.rglob("*") if is_media_file(p)]
    if not media_files:
        emit(job_id, "❌ No downloadable media files were produced")
        finish(job_id, "error")
        return

    is_playlist = mode == "playlist" or len(media_files) > 1
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["is_playlist"] = is_playlist
            jobs[job_id]["item_count"] = len(media_files)

    if is_playlist:
        register_playlist_artifact(job_id, job_dir, title, duplicate_action, partial=partial)
    else:
        register_single_artifact(job_id, media_files[0], duplicate_action, partial=partial)


def apply_duplicate_policy_before_start(job_id: str, title: str, fmt: str, duplicate_action: str) -> bool:
    if duplicate_action != "reuse":
        return False
    matches = find_duplicates(title, fmt)
    if not matches:
        return False
    path = Path(matches[0]["path"])
    if path.is_dir():
        finish(job_id, "done", filename=str(path), serve_path=None, artifacts=[matches[0]], duplicate_used=True, is_playlist=True)
    else:
        finish(job_id, "done", filename=str(path), serve_path=str(path), artifacts=[matches[0]], duplicate_used=True, is_playlist=False)
    emit(job_id, f"♻ Reused cached download: {path.name}")
    return True


def run_ytdlp(job_id: str, url: str, fmt: str, quality: str, mode: str, duplicate_action: str, embed_metadata: bool = True, advanced: dict | None = None):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            cleanup_job_dir(job_dir)
            return
        title = job.get("title") or "download"
        job["progress"] = 0
        job["last_activity"] = utc_now()

    try:
        if apply_duplicate_policy_before_start(job_id, title, fmt, duplicate_action):
            return

        output_template = str(job_dir / "%(title).200B [%(id)s].%(ext)s")
        cmd = [
            "yt-dlp", "--newline", "--progress", "--no-part", "--restrict-filenames",
            "--windows-filenames", "--print", "before_dl:%(title)s", 
            "--remote-components", "ejs:github",
            "-o", output_template,
        ]
        if mode == "single":
            cmd.append("--no-playlist")
        elif mode == "playlist":
            cmd += ["--yes-playlist", "--ignore-errors"]

        if embed_metadata:
            cmd += ["--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg"]

        advanced = advanced or {}
        if advanced.get("write_subs"):
            cmd += ["--write-subs", "--sub-langs", "all"]
        if advanced.get("rate_limit"):
            cmd += ["--limit-rate", str(advanced["rate_limit"]) ]

        if fmt == "mp3":
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", quality]
        elif fmt == "mp4":
            cmd += ["-f", f"bv*[height<={quality}]+ba/b[height<={quality}]/b", "--merge-output-format", "mp4"]
        elif fmt == "opus":
            cmd += ["-x", "--audio-format", "opus"]
        else:
            cmd += ["-f", "bv*+ba/b"]
        extra_args = advanced.get("extra_args") if isinstance(advanced, dict) else ""
        if extra_args:
            cmd += shlex.split(extra_args)
        cmd.append(url)

        code = run_process(job_id, cmd)
        if code != 0:
            media_files = [p for p in job_dir.rglob("*") if is_media_file(p)]
            if mode == "playlist" and media_files:
                emit(job_id, f"⚠ yt-dlp exited with code {code}, but {len(media_files)} media file(s) were downloaded. Saving partial playlist folder.")
                finalize_outputs(job_id, job_dir, title, mode, duplicate_action, partial=True)
                return
            emit(job_id, f"❌ yt-dlp exited with code {code}")
            finish(job_id, "error")
            return
        finalize_outputs(job_id, job_dir, title, mode, duplicate_action)
    except Exception as exc:
        emit(job_id, f"❌ Unexpected yt-dlp error: {exc}")
        finish(job_id, "error")
    finally:
        cleanup_job_dir(job_dir)


def run_spotdl(job_id: str, url: str, fmt: str, mode: str, duplicate_action: str, advanced: dict | None = None):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            cleanup_job_dir(job_dir)
            return
        title = job.get("title") or "Spotify Media"
        job["progress"] = 0
        job["last_activity"] = utc_now()

    try:
        if apply_duplicate_policy_before_start(job_id, title, fmt, duplicate_action):
            return

        output_template = str(job_dir / "{artists} - {title}.{output-ext}")
        cmd = [
            "spotdl", "download", url,
            "--output", output_template,
            "--format", fmt if fmt in {"mp3", "opus", "flac", "ogg"} else "mp3",
        ]
        advanced = advanced or {}
        if advanced.get("bitrate"):
            cmd += ["--bitrate", str(advanced["bitrate"])]
        if advanced.get("threads"):
            cmd += ["--threads", str(advanced["threads"])]
        if advanced.get("audio_provider"):
            cmd += ["--audio", str(advanced["audio_provider"])]
        if advanced.get("yt_dlp_args"):
            cmd += ["--yt-dlp-args", str(advanced["yt_dlp_args"])]

        # Pass credentials if available in environment
        client_id = os.environ.get("SPOTIPY_CLIENT_ID")
        client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET")
        genius_token = os.environ.get("GENIUS_ACCESS_TOKEN")

        if client_id:
            cmd += ["--client-id", client_id]
        if client_secret:
            cmd += ["--client-secret", client_secret]
        if genius_token:
            cmd += ["--genius-access-token", genius_token]

        code = run_process(job_id, cmd)
        if code != 0:
            emit(job_id, f"❌ spotdl failed with code {code}")
            finish(job_id, "error")
            return
        finalize_outputs(job_id, job_dir, title, mode, duplicate_action)
    except Exception as exc:
        emit(job_id, f"❌ Unexpected spotdl error: {exc}")
        finish(job_id, "error")
    finally:
        cleanup_job_dir(job_dir)


def background_cleanup():
    while True:
        time.sleep(60)
        now = datetime.now(UTC).replace(tzinfo=None)
        for directory, max_age in ((SERVE_DIR, timedelta(hours=6)), (WORK_DIR, timedelta(hours=2))):
            try:
                for path in directory.iterdir():
                    mtime = datetime.fromtimestamp(path.stat().st_mtime)
                    if now - mtime > max_age:
                        shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
            except Exception:
                pass

        timeout_job_ids = []
        with jobs_lock:
            for job in jobs.values():
                if job.get("status") == "running":
                    if job.get("last_activity"):
                        last_activity = datetime.fromisoformat(job["last_activity"])
                        if now - last_activity > timedelta(minutes=20):
                            job["status"] = "error"
                            job["log"].append("❌ Job timed out (no activity for 20 minutes)")
                            job["finished_at"] = now.isoformat()
                            job.pop("proc", None)
                            timeout_job_ids.append(job["id"])

        for job_id in timeout_job_ids:
            try:
                save_job_to_db(job_id)
            except Exception:
                pass


threading.Thread(target=background_cleanup, daemon=True).start()


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    source = detect_source(url)
    if source == "spotify":
        try:
            cmd = ["spotdl", "save", url, "--save-file", "-"]
            client_id = os.environ.get("SPOTIPY_CLIENT_ID")
            client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET")
            if client_id:
                cmd += ["--client-id", client_id]
            if client_secret:
                cmd += ["--client-secret", client_secret]

            # Use a longer timeout for info fetching as spotdl can be slow
            output = subprocess.check_output(cmd, text=True, timeout=60)
            json_start = output.find("[")
            if json_start != -1:
                metadata = json.loads(output[json_start:])
                if metadata:
                    entry = metadata[0]
                    # Prefer list_name (playlist/album) if available, otherwise Song Name
                    title = entry.get("list_name") or f"{entry.get('artist')} - {entry.get('name')}"
                    return jsonify({
                        "title": title,
                        "source": source,
                        "is_playlist": infer_mode(url) == "playlist",
                        "item_count": len(metadata),
                        "mode": infer_mode(url)
                    })
        except Exception as exc:
            app.logger.error(f"Failed to fetch Spotify metadata: {exc}")

        return jsonify({"title": resolve_job_title(url, source), "source": source, "is_playlist": infer_mode(url) == "playlist", "mode": infer_mode(url)})

    try:
        cmd = ["yt-dlp", "--dump-single-json", "--flat-playlist", "--no-warnings", url]
        output = subprocess.check_output(cmd, text=True, timeout=30)
        info = json.loads(output)
        entries = info.get("entries") or []
        is_playlist = info.get("_type") == "playlist" or len(entries) > 1
        return jsonify({
            "title": resolve_job_title(url, source, info.get("title") or info.get("playlist_title")),
            "source": source,
            "is_playlist": is_playlist,
            "item_count": len(entries) if entries else (1 if not is_playlist else None),
            "mode": "playlist" if is_playlist else "single",
        })
    except Exception as exc:
        return jsonify({"title": resolve_job_title(url, source), "source": source, "error": str(exc), "is_playlist": infer_mode(url) == "playlist", "mode": infer_mode(url)}), 200


@app.route("/api/duplicates", methods=["POST"])
def check_duplicates():
    data = request.json or {}
    title = data.get("title") or data.get("url") or ""
    fmt = data.get("format")
    return jsonify({"duplicates": find_duplicates(title, fmt)})


@app.route("/api/clear", methods=["POST"])
def clear_jobs():
    client_id = request.headers.get("X-Client-ID")
    to_delete = []
    with jobs_lock:
        for job_id, job in list(jobs.items()):
            if job.get("client_id") == client_id and job["status"] != "running":
                to_delete.append(job_id)
                jobs.pop(job_id)

    with sqlite3.connect(DB_PATH) as conn:
        for job_id in to_delete:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    return jsonify({"ok": True, "cleared": len(to_delete)})


@app.route("/api/download", methods=["POST"])
def start_download():
    client_id = request.headers.get("X-Client-ID")
    data = request.json or {}
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")
    source = detect_source(url)
    mode = data.get("mode") or "auto"
    if mode == "auto":
        mode = infer_mode(url)
    duplicate_action = data.get("duplicate_action", "again")
    if duplicate_action not in {"again", "override", "reuse"}:
        duplicate_action = "again"
    title = resolve_job_title(url, source, data.get("title"))
    embed_metadata = bool(data.get("embed_metadata", True))
    advanced = data.get("advanced") if isinstance(data.get("advanced"), dict) else {}

    if not url:
        return jsonify({"error": "URL is required"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "client_id": client_id,
            "url": url,
            "source": source,
            "format": fmt,
            "quality": quality,
            "title": title,
            "mode": mode,
            "is_playlist": mode == "playlist",
            "duplicate_action": duplicate_action,
            "status": "running",
            "progress": 0,
            "log": [],
            "filename": None,
            "serve_path": None,
            "artifacts": [],
            "partial": False,
            "started_at": utc_now(),
            "finished_at": None,
            "last_activity": utc_now(),
        }
    save_job_to_db(job_id)

    target = run_spotdl if source == "spotify" else run_ytdlp
    args = (job_id, url, fmt, mode, duplicate_action, advanced) if source == "spotify" else (job_id, url, fmt, quality, mode, duplicate_action, embed_metadata, advanced)
    threading.Thread(target=target, args=args, daemon=True).start()
    return jsonify({"job_id": job_id}), 202
@app.route("/api/jobs/<job_id>/stop", methods=["POST"])
def stop_job(job_id):
    client_id = request.headers.get("X-Client-ID")
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("client_id") != client_id:
            return jsonify({"error": "Job not found"}), 404
        proc = job.get("proc")
        if job["status"] == "running" and proc:
            try:
                proc.terminate()
                job["status"] = "stopped"
                job["log"].append("🛑 Job stopped by user")
            except Exception:
                pass
    return jsonify({"ok": True})


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    client_id = request.headers.get("X-Client-ID")
    with jobs_lock:
        user_jobs = [job for job in jobs.values() if job.get("client_id") == client_id]
        return jsonify([serializable_job(job) for job in reversed(user_jobs)][:50])


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Not found"}), 404
        return jsonify(serializable_job(job))


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
    return send_file(serve_path, as_attachment=True, download_name=Path(serve_path).name)


@app.route("/api/files", methods=["GET"])
def list_all_files():
    client_id = request.headers.get("X-Client-ID")
    files = []
    
    # Query only files "owned" by this client_id
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT filename, path FROM downloads WHERE client_id = ?", (client_id,))
        user_files = cursor.fetchall()

    for filename, path_str in user_files:
        path = Path(path_str)
        if path.exists():
            stat = path.stat()
            files.append({
                "name": path.name,
                "is_dir": path.is_dir(),
                "size": stat.st_size if path.is_file() else sum(f.stat().st_size for f in path.rglob("*") if f.is_file()),
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    # Sort by mtime descending
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(files)


@app.route("/api/download-direct", methods=["GET"])
def download_direct():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "Name is required"}), 400
    
    try:
        # Security: prevent path traversal
        download_dir_abs = DOWNLOAD_DIR.resolve()
        safe_path = (download_dir_abs / name).resolve()
        
        # Robust path traversal check
        if not safe_path.is_relative_to(download_dir_abs):
             app.logger.warning(f"Blocked path traversal attempt: {name}")
             return jsonify({"error": "Invalid file path"}), 403
             
        if not safe_path.exists():
            app.logger.warning(f"File not found: {safe_path}")
            return jsonify({"error": "File not found"}), 404

        if safe_path.is_file():
            # Serve directly from permanent storage
            return send_file(safe_path, as_attachment=True, download_name=safe_path.name)
        else:
            if request.args.get("zip", "").lower() in {"1", "true", "yes"}:
                # Stream the folder as a ZIP archive on-the-fly
                filename = f"{safe_path.name}.zip"
                return Response(
                    stream_folder_as_zip(safe_path),
                    mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename={filename}"}
                )
            return jsonify({"error": "Directory downloads are disabled by default. Request zip=true to download an archive."}), 400
    except Exception as e:
        app.logger.error(f"Error in download_direct: {e}", exc_info=True)
        return jsonify({"error": "Internal server error", "message": str(e)}), 500


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    client_id = request.headers.get("X-Client-ID")
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("client_id") != client_id:
            return jsonify({"error": "Not found"}), 404
        jobs.pop(job_id)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    serve_path = job.get("serve_path")
    if serve_path:
        try:
            Path(serve_path).unlink(missing_ok=True)
        except Exception:
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
        },
        "modes": ["auto", "single", "playlist"],
        "duplicate_actions": ["reuse", "again", "override"],
    })


@app.route("/api/health", methods=["GET"])
def health_check():
    try:
        ytdlp_version = subprocess.check_output(["yt-dlp", "--version"], text=True, timeout=10).strip()
    except Exception:
        ytdlp_version = "not found"
    try:
        spotdl_version = subprocess.check_output(["spotdl", "--version"], text=True, timeout=10).strip()
    except Exception:
        spotdl_version = "not found"
    return jsonify({"status": "ok", "ytdlp_version": ytdlp_version, "spotdl_version": spotdl_version, "jobs_count": len(jobs)})


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(Exception)
def handle_exception(e):
    # Log the error and stacktrace
    app.logger.error(f"Unhandled Exception: {e}", exc_info=True)
    # Return JSON instead of the default HTML error page
    return jsonify({"error": "Internal server error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
