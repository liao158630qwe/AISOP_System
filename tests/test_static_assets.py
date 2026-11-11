"""Public URLs keep working when the frontend is stored outside the repository root."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import pytest


class PageAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.urls.append(attrs['src'])
        if tag == 'link' and attrs.get('href') and attrs.get('rel') in ('stylesheet', 'icon'):
            self.urls.append(attrs['href'])


@pytest.mark.parametrize('url', [
    '/', '/admin', '/training', '/datasets', '/settings', '/scene-console.html',
    '/screw-inspection.html', '/station-one.html?embed=1', '/board', '/board.html',
])
def test_pages_and_their_local_dependencies(context, url):
    _, _, client = context
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    parser = PageAssets()
    parser.feed(response.text)
    assert parser.urls
    for resource in parser.urls:
        if urlsplit(resource).scheme or resource.startswith('//'):
            continue
        path = urljoin(url, resource)
        asset = client.get(path)
        assert asset.status_code == 200, path
        assert asset.content, path
        assert not asset.headers['content-type'].startswith('text/html'), path


@pytest.mark.parametrize('url', [
    '/frontend/index.html', '/backend/app.py', '/requirements-api.txt',
    '/docs/README.md', '/models/test-models.json', '/assets/../backend/app.py',
])
def test_source_and_private_files_are_not_published(context, url):
    assert context[2].get(url).status_code == 404


def test_station_frame_encoder_worker_is_served_as_javascript(context):
    response = context[2].get('/station-frame-worker.js')
    assert response.status_code == 200
    assert 'javascript' in response.headers['content-type']
    assert 'no-store' in response.headers['cache-control']
