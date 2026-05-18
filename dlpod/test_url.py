import requests
import time
import sys
import subprocess
import os

def test_url(url):
    base_url = "http://127.0.0.1:5000"
    
    print(f"--- Testing URL: {url} ---")
    
    # 1. Get Info
    print("Testing /api/info...")
    try:
        resp = requests.post(f"{base_url}/api/info", json={"url": url})
        if resp.status_code != 200:
            print(f"FAILED: /api/info returned {resp.status_code}")
            print(resp.text)
            return
        info = resp.json()
        print(f"SUCCESS: Info retrieved: {info.get('title')} ({info.get('source')})")
    except Exception as e:
        print(f"ERROR calling /api/info: {e}")
        return

    # 2. Start Download
    print("Testing /api/download...")
    try:
        resp = requests.post(f"{base_url}/api/download", json={"url": url, "format": "mp3"})
        if resp.status_code != 202:
            print(f"FAILED: /api/download returned {resp.status_code}")
            print(resp.text)
            return
        job_id = resp.json()["job_id"]
        print(f"SUCCESS: Job started with ID: {job_id}")
    except Exception as e:
        print(f"ERROR calling /api/download: {e}")
        return

    # 3. Poll for Completion
    print("Polling job status...")
    start_time = time.time()
    timeout = 300 # 5 minutes
    while time.time() - start_time < timeout:
        try:
            resp = requests.get(f"{base_url}/api/jobs/{job_id}")
            job = resp.json()
            status = job["status"]
            progress = job["progress"]
            print(f"Status: {status} ({progress}%)", end="\r")
            
            if status == "done":
                print(f"\nSUCCESS: Download completed in {int(time.time() - start_time)}s")
                print(f"Artifacts: {job['artifacts']}")
                return
            if status == "error":
                print(f"\nFAILED: Job failed with status 'error'")
                # Try to get logs
                log_resp = requests.get(f"{base_url}/api/jobs/{job_id}/log")
                print("Logs:")
                for line in log_resp.json().get("log", []):
                    print(f"  {line}")
                return
        except Exception as e:
            print(f"\nERROR polling status: {e}")
            return
        time.sleep(2)
    
    print(f"\nFAILED: Job timed out after {timeout}s")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 test_url.py <URL>")
        sys.exit(1)
    
    target_url = sys.argv[1]
    
    # Start server in background if not running
    server_process = None
    try:
        requests.get("http://127.0.0.1:5000/api/health")
        print("Server is already running.")
    except:
        print("Starting server...")
        env = os.environ.copy()
        env["DOWNLOAD_DIR"] = "/tmp/dlpod_test_runs"
        os.makedirs(env["DOWNLOAD_DIR"], exist_ok=True)
        server_process = subprocess.Popen(["./venv/bin/python3", "app/main.py"], env=env)
        time.sleep(3) # Wait for server to start
    
    try:
        test_url(target_url)
    finally:
        if server_process:
            print("Stopping server...")
            server_process.terminate()
            server_process.wait()
