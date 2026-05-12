import pytest
import os
import json
from main import app, jobs

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_health_endpoint(client):
    """Test the health check endpoint."""
    response = client.get('/api/health')
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'ok'
    assert 'ytdlp_version' in data

def test_get_formats(client):
    """Test the formats endpoint."""
    response = client.get('/api/formats')
    assert response.status_code == 200
    data = response.get_json()
    assert 'yt' in data
    assert 'spotify' in data
    assert 'mp3' in data['qualities']

def test_list_jobs_empty(client):
    """Test listing jobs when none exist."""
    jobs.clear()
    response = client.get('/api/jobs')
    assert response.status_code == 200
    assert response.get_json() == []

def test_start_download_no_url(client):
    """Test starting a download without a URL."""
    response = client.post('/api/download', json={})
    assert response.status_code == 400
    assert 'error' in response.get_json()

def test_job_lifecycle_flow(client):
    """Test the creation and deletion of a job."""
    jobs.clear()
    
    # Create job
    response = client.post('/api/download', json={'url': 'https://youtube.com/watch?v=dQw4w9WgXcQ'})
    assert response.status_code == 202
    job_id = response.get_json()['job_id']
    
    # Get job status
    response = client.get(f'/api/jobs/{job_id}')
    assert response.status_code == 200
    assert response.get_json()['id'] == job_id
    
    # Delete job
    response = client.delete(f'/api/jobs/{job_id}')
    assert response.status_code == 200
    assert response.get_json()['ok'] == True
    
    # Verify deleted
    response = client.get(f'/api/jobs/{job_id}')
    assert response.status_code == 404
