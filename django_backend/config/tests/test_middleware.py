"""
Tests for config/middleware.py's frontend_cache_headers() — the
WHITENOISE_ADD_HEADERS_FUNCTION hook that forces .html/.js/.css to always
revalidate with the server instead of trusting WhiteNoise's default 60s
Cache-Control. Added after a stale-browser-cache incident let a client keep
calling a since-retired API endpoint after a deploy removed it.
"""
from django.test import SimpleTestCase

from config.middleware import frontend_cache_headers


class FrontendCacheHeadersTests(SimpleTestCase):
    def test_js_file_forces_revalidation(self):
        headers = {'Cache-Control': 'max-age=60, public'}
        frontend_cache_headers(headers, '/app/frontend/js/generate-tds.js', '/js/generate-tds.js')
        self.assertEqual(headers['Cache-Control'], 'no-cache, public')

    def test_html_file_forces_revalidation(self):
        headers = {'Cache-Control': 'max-age=60, public'}
        frontend_cache_headers(headers, '/app/frontend/generate-tds.html', '/generate-tds.html')
        self.assertEqual(headers['Cache-Control'], 'no-cache, public')

    def test_css_file_forces_revalidation(self):
        headers = {'Cache-Control': 'max-age=60, public'}
        frontend_cache_headers(headers, '/app/frontend/css/style.css', '/css/style.css')
        self.assertEqual(headers['Cache-Control'], 'no-cache, public')

    def test_image_keeps_default_caching(self):
        headers = {'Cache-Control': 'max-age=60, public'}
        frontend_cache_headers(headers, '/app/frontend/img/hero.png', '/img/hero.png')
        self.assertEqual(headers['Cache-Control'], 'max-age=60, public')
