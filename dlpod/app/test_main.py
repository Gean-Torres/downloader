import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import main
from main import app, jobs


@pytest.fixture
def client(tmp_path, monkeypatch):
    app.config['TESTING'] = True
    monkeypatch.setattr(main, 'DOWNLOAD_DIR', tmp_path)
    monkeypatch.setattr(main, 'SERVE_DIR', tmp_path / '_serve')
    monkeypatch.setattr(main, 'WORK_DIR', tmp_path / '_work')
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'data' / 'dlpod.db')
    main.SERVE_DIR.mkdir(parents=True, exist_ok=True)
    main.WORK_DIR.mkdir(parents=True, exist_ok=True)
    main.DATA_DIR.mkdir(parents=True, exist_ok=True)
    main.init_db()
    jobs.clear()
    with app.test_client() as client:
        yield client
    jobs.clear()


def test_health_endpoint(client, monkeypatch):
    """Test the health check endpoint."""
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: 'tool-version')
    response = client.get('/api/health')
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'ok'
    assert 'ytdlp_version' in data
    assert 'spotdl_version' in data


def test_get_formats(client):
    """Test the formats endpoint."""
    response = client.get('/api/formats')
    assert response.status_code == 200
    data = response.get_json()
    assert 'yt' in data
    assert 'spotify' in data
    assert 'mp3' in data['qualities']
    assert 'single' in data['modes']
    assert 'reuse' in data['duplicate_actions']


def test_list_jobs_empty(client):
    """Test listing jobs when none exist."""
    response = client.get('/api/jobs')
    assert response.status_code == 200
    assert response.get_json() == []


def test_list_jobs_omits_process_handles(client):
    jobs['abc'] = {
        'id': 'abc',
        'url': 'https://example.test/video',
        'source': 'yt',
        'format': 'mp3',
        'quality': '192',
        'title': 'Example',
        'mode': 'single',
        'is_playlist': False,
        'duplicate_action': 'again',
        'status': 'running',
        'progress': 0,
        'log': [],
        'filename': None,
        'serve_path': None,
        'artifacts': [],
        'started_at': main.utc_now(),
        'finished_at': None,
        'last_activity': main.utc_now(),
        'proc': Mock(),
    }
    response = client.get('/api/jobs')
    assert response.status_code == 200
    data = response.get_json()[0]
    assert 'proc' not in data
    assert data['download_url'] is None


def test_start_download_no_url(client):
    """Test starting a download without a URL."""
    response = client.post('/api/download', json={})
    assert response.status_code == 400
    assert 'error' in response.get_json()


def test_start_download_rejects_non_url_input(client):
    response = client.post('/api/download', json={'url': 'podman run -d --name exporter'})
    assert response.status_code == 400
    assert response.get_json()['error'] == 'A valid http(s) URL is required'


def test_info_rejects_non_url_input(client):
    response = client.post('/api/info', json={'url': 'Gibson Custom 1957 SJ-200 Reissue'})
    assert response.status_code == 400
    assert response.get_json()['error'] == 'A valid http(s) URL is required'


def test_unknown_route_remains_404(client):
    response = client.get('/api/does-not-exist')
    assert response.status_code == 404


def test_job_lifecycle_flow(client, monkeypatch):
    """Test the creation and deletion of a job without launching external download tools."""
    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args

        def start(self):
            job_id = self.args[0]
            main.finish(job_id, 'stopped')

    monkeypatch.setattr(main.threading, 'Thread', ImmediateThread)

    response = client.post('/api/download', json={'url': 'https://youtube.com/watch?v=dQw4w9WgXcQ'})
    assert response.status_code == 202
    job_id = response.get_json()['job_id']

    response = client.get(f'/api/jobs/{job_id}')
    assert response.status_code == 200
    assert response.get_json()['id'] == job_id

    response = client.delete(f'/api/jobs/{job_id}')
    assert response.status_code == 200
    assert response.get_json()['ok'] is True

    response = client.get(f'/api/jobs/{job_id}')
    assert response.status_code == 404


def test_download_endpoint_serves_completed_artifact(client, tmp_path):
    media = main.SERVE_DIR / 'song.mp3'
    media.write_bytes(b'audio')
    jobs['done'] = {
        'id': 'done',
        'url': 'https://example.test/video',
        'source': 'yt',
        'format': 'mp3',
        'quality': '192',
        'title': 'song',
        'mode': 'single',
        'is_playlist': False,
        'duplicate_action': 'again',
        'status': 'done',
        'progress': 100,
        'log': [],
        'filename': str(media),
        'serve_path': str(media),
        'artifacts': [],
        'started_at': main.utc_now(),
        'finished_at': main.utc_now(),
        'last_activity': main.utc_now(),
    }

    response = client.get('/api/jobs/done/download')
    assert response.status_code == 200
    assert response.data == b'audio'
    assert 'attachment' in response.headers['Content-Disposition']


def test_duplicates_endpoint_detects_cached_media(client):
    cached = main.DOWNLOAD_DIR / 'Example Song.mp3'
    cached.write_bytes(b'audio')

    response = client.post('/api/duplicates', json={'title': 'Example Song', 'format': 'mp3'})
    assert response.status_code == 200
    duplicates = response.get_json()['duplicates']
    assert len(duplicates) == 1
    assert duplicates[0]['name'] == 'Example Song.mp3'
    assert duplicates[0]['cached'] is True


def make_running_job(job_id='job', mode='playlist'):
    jobs[job_id] = {
        'id': job_id,
        'url': 'https://youtube.com/playlist?list=PLtest',
        'source': 'yt',
        'format': 'mp3',
        'quality': '192',
        'title': 'Playlist Title',
        'mode': mode,
        'is_playlist': mode == 'playlist',
        'duplicate_action': 'again',
        'status': 'running',
        'progress': 0,
        'log': [],
        'filename': None,
        'serve_path': None,
        'artifacts': [],
        'partial': False,
        'started_at': main.utc_now(),
        'finished_at': None,
        'last_activity': main.utc_now(),
    }


def test_ytdlp_playlist_uses_ignore_errors(client, monkeypatch):
    captured = {}

    def fake_run_process(job_id, cmd):
        captured['cmd'] = cmd
        return 0

    monkeypatch.setattr(main, 'run_process', fake_run_process)
    make_running_job()

    main.run_ytdlp('job', 'https://youtube.com/playlist?list=PLtest', 'mp3', '192', 'playlist', 'again')

    assert '--yes-playlist' in captured['cmd']
    assert '--ignore-errors' in captured['cmd']


def test_refresh_endpoint(client, tmp_path):
    # Create an untracked file
    (tmp_path / "manual_file.mp3").write_bytes(b"data")

    response = client.post("/api/refresh")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    # Check if it appears in duplicates (which uses DB now)
    response = client.post("/api/duplicates", json={"title": "manual_file"})
    assert response.status_code == 200
    duplicates = response.get_json()["duplicates"]
    assert len(duplicates) == 1
    assert duplicates[0]["name"] == "manual_file.mp3"


def test_duplicates_endpoint_ignores_placeholder_title(client):
    cached = main.DOWNLOAD_DIR / 'download.mp3'
    cached.write_bytes(b'audio')

    response = client.post('/api/duplicates', json={'title': 'download', 'format': 'mp3'})

    assert response.status_code == 200
    assert response.get_json()['duplicates'] == []


def test_reuse_duplicate_policy_prefers_exact_url_for_placeholder_title(client):
    cached = main.DOWNLOAD_DIR / 'download.mp3'
    cached.write_bytes(b'audio')
    with main.sqlite3.connect(main.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO downloads (url, client_id, title, filename, format, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('https://example.test/original', 'client', 'download', cached.name, 'mp3', str(cached), main.utc_now())
        )

    make_running_job('placeholder-job', mode='single')
    jobs['placeholder-job']['title'] = 'download'
    jobs['placeholder-job']['url'] = 'https://example.test/new-link'

    reused = main.apply_duplicate_policy_before_start(
        'placeholder-job', 'https://example.test/new-link', 'download', 'mp3', 'reuse'
    )

    assert reused is False
    assert jobs['placeholder-job']['status'] == 'running'


def test_reuse_duplicate_policy_allows_exact_url_with_placeholder_title(client):
    cached = main.DOWNLOAD_DIR / 'download.mp3'
    cached.write_bytes(b'audio')
    with main.sqlite3.connect(main.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO downloads (url, client_id, title, filename, format, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('https://example.test/original', 'client', 'download', cached.name, 'mp3', str(cached), main.utc_now())
        )

    make_running_job('exact-url-job', mode='single')
    jobs['exact-url-job']['title'] = 'download'
    jobs['exact-url-job']['url'] = 'https://example.test/original'

    reused = main.apply_duplicate_policy_before_start(
        'exact-url-job', 'https://example.test/original', 'download', 'mp3', 'reuse'
    )

    assert reused is True
    assert jobs['exact-url-job']['status'] == 'done'
    assert jobs['exact-url-job']['duplicate_used'] is True


def test_register_login_and_password_change(client):
    response = client.post('/api/auth/register', json={
        'username': 'tester',
        'email': 'tester@example.test',
        'password': 'password123',
    })
    assert response.status_code == 201
    data = response.get_json()
    token = data['token']
    assert data['user']['username'] == 'tester'
    assert data['user']['is_admin'] is False

    response = client.get('/api/auth/me', headers={'X-Auth-Token': token})
    assert response.status_code == 200
    assert response.get_json()['user']['email'] == 'tester@example.test'

    response = client.post('/api/auth/change-password', headers={'X-Auth-Token': token}, json={
        'current_password': 'password123',
        'new_password': 'new-password',
    })
    assert response.status_code == 200
    assert response.get_json()['ok'] is True

    response = client.post('/api/auth/login', json={'identifier': 'tester', 'password': 'new-password'})
    assert response.status_code == 200
    assert response.get_json()['user']['username'] == 'tester'


def test_hardcoded_admin_username_can_open_administration(client):
    response = client.post('/api/auth/register', json={
        'username': 'Gean-Torres',
        'email': 'gean@example.test',
        'password': 'admin-pass',
    })
    assert response.status_code == 201
    data = response.get_json()
    assert data['user']['is_admin'] is True

    response = client.get('/api/admin/users', headers={'X-Auth-Token': data['token']})
    assert response.status_code == 200
    users = response.get_json()
    assert users[0]['username'] == 'Gean-Torres'
    assert users[0]['is_admin'] is True


def test_logged_in_downloads_use_account_client_id(client, monkeypatch):
    response = client.post('/api/auth/register', json={
        'username': 'accounted',
        'email': 'accounted@example.test',
        'password': 'password123',
    })
    token = response.get_json()['token']

    def fake_put(task):
        return None

    monkeypatch.setattr(main.task_queue, 'put', fake_put)
    response = client.post(
        '/api/download',
        headers={'X-Client-ID': 'browser-client', 'X-Auth-Token': token},
        json={'url': 'https://youtube.com/watch?v=dQw4w9WgXcQ'},
    )
    assert response.status_code == 202
    job_id = response.get_json()['job_id']
    assert jobs[job_id]['client_id'].startswith('user:')

    response = client.get('/api/jobs', headers={'X-Client-ID': 'other-browser', 'X-Auth-Token': token})
    assert response.status_code == 200
    assert response.get_json()[0]['id'] == job_id


def test_email_recovery_placeholder(client):
    response = client.post('/api/auth/recover', json={'email': 'tester@example.test'})
    assert response.status_code == 501
    assert response.get_json()['error'] == 'Email recovery is not implemented yet'
