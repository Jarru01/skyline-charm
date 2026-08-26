"""Tests for nginx config injection methods (string processing)."""

import textwrap

from charm import SkylineCharm


class TestInjectHealthEndpoint:
    """_inject_health_endpoint() adds /healthz to generated nginx config."""

    def test_injects_health_block(self, harness):
        harness.begin()
        content = textwrap.dedent("""\
            http {
                server {
                    location / {
                        try_files $uri $uri/ /index.html;
                    }
                }
            }
        """)
        result = harness.charm._inject_health_endpoint(content)
        assert "# skyline-charm: lb health endpoint" in result
        assert "location = /healthz" in result
        assert "proxy_pass http://127.0.0.1:28000/" in result
        assert "location @healthy" in result
        health_pos = result.index("location = /healthz")
        root_pos = result.index("location /")
        assert health_pos < root_pos

    def test_idempotent(self, harness):
        harness.begin()
        content = textwrap.dedent("""\
            http {
                server {
                    # skyline-charm: lb health endpoint
                    location = /healthz {
                        proxy_pass http://127.0.0.1:28000/;
                    }
                    location @healthy {
                        return 200 "ok\\n";
                    }

                    location / {
                        try_files $uri $uri/ /index.html;
                    }
                }
            }
        """)
        result = harness.charm._inject_health_endpoint(content)
        assert result == content

    def test_no_anchor_returns_unchanged(self, harness):
        harness.begin()
        content = "http { server { listen 80; } }"
        result = harness.charm._inject_health_endpoint(content)
        assert result == content


class TestInjectStaticCacheControl:
    """_inject_static_cache_control() adds cache headers for static assets."""

    def test_injects_cache_block(self, harness):
        harness.begin()
        content = textwrap.dedent("""\
            http {
                server {
                    location / {
                        try_files $uri $uri/ /index.html;
                    }
                }
            }
        """)
        result = harness.charm._inject_static_cache_control(content)
        assert "# skyline-charm: static-asset cache-control" in result
        assert "location ~* \\.(js|css|png" in result
        assert 'Cache-Control "public, must-revalidate"' in result
        assert "expires 7d" in result

    def test_idempotent(self, harness):
        harness.begin()
        content = textwrap.dedent("""\
            http {
                server {
                    # skyline-charm: static-asset cache-control
                    location ~* \\.(js|css|png)$ {
                        expires 7d;
                        add_header Cache-Control "public, must-revalidate";
                        try_files $uri =404;
                    }

                    location / {
                        try_files $uri $uri/ /index.html;
                    }
                }
            }
        """)
        result = harness.charm._inject_static_cache_control(content)
        assert result == content

    def test_no_anchor_returns_unchanged(self, harness):
        harness.begin()
        content = "http { server { listen 80; } }"
        result = harness.charm._inject_static_cache_control(content)
        assert result == content

    def test_both_injections_together(self, harness):
        harness.begin()
        content = textwrap.dedent("""\
            http {
                server {
                    location / {
                        try_files $uri $uri/ /index.html;
                    }
                }
            }
        """)
        result = harness.charm._inject_health_endpoint(content)
        result = harness.charm._inject_static_cache_control(result)
        assert "# skyline-charm: lb health endpoint" in result
        assert "# skyline-charm: static-asset cache-control" in result
        health_pos = result.index("location = /healthz")
        cache_pos = result.index("location ~*")
        root_pos = result.index("location /")
        assert health_pos < cache_pos < root_pos
