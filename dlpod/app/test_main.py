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
    main.SERVE_DIR.mkdir(parents=True, exist_ok=True)
    main.WORK_DIR.mkdir(parents=True, exist_ok=True)
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


def test_ytdlp_playlist_nonzero_with_media_saves_partial_archive(client, monkeypatch):
    def fake_run_process(job_id, cmd):
        job_dir = main.WORK_DIR / job_id
        (job_dir / 'Song One [abc].mp3').write_bytes(b'audio')
        return 1

    monkeypatch.setattr(main, 'run_process', fake_run_process)
    make_running_job()

    main.run_ytdlp('job', 'https://youtube.com/playlist?list=PLtest', 'mp3', '192', 'playlist', 'again')

    job = jobs['job']
    assert job['status'] == 'done'
    assert job['partial'] is True
    assert job['is_playlist'] is True
    assert job['item_count'] == 1
    assert Path(job['serve_path']).suffix == '.zip'
    assert any('partial playlist archive' in line for line in job['log'])
