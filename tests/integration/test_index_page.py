"""Integration tests for index page + static asset reachability (Task 4.3).

Contract source: ``docs/design/issue-3-m4-frontend-ui.md`` §3.2, §4, §9.

RED until templates + static files exist.
"""
from __future__ import annotations

# ===========================================================================
# #16. base.html includes viewport meta
# ===========================================================================


async def test_base_html_includes_viewport_meta(app_client):
    resp = await app_client.get("/login")
    assert resp.status_code == 200
    body = resp.text
    # Must contain the responsive viewport meta tag.
    assert 'name="viewport"' in body
    assert "width=device-width" in body


# ===========================================================================
# #17. /static/style.css reachable
# ===========================================================================


async def test_static_style_css_reachable(app_client):
    resp = await app_client.get("/static/style.css")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    assert "text/css" in ctype


# ===========================================================================
# #18. /static/app.js reachable
# ===========================================================================


async def test_static_app_js_reachable(app_client):
    resp = await app_client.get("/static/app.js")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    # Starlette's StaticFiles uses python's mimetypes — .js → application/javascript
    # or text/javascript depending on platform.
    assert "javascript" in ctype


# ===========================================================================
# #19. /static/vendor/marked.min.js reachable
# ===========================================================================


async def test_static_marked_min_js_reachable(app_client):
    resp = await app_client.get("/static/vendor/marked.min.js")
    assert resp.status_code == 200


# ===========================================================================
# #20. index page has file drop zone
# ===========================================================================


async def test_index_html_has_file_drop_zone(app_client, auth_session):
    _user, raw = await auth_session("drop@example.com")
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/")
    app_client.cookies.clear()

    body = resp.text
    assert 'id="drop-zone"' in body
    assert 'id="file-input"' in body
    # accept attribute lists at least the 4 expected extensions
    assert 'accept=' in body
    for ext in (".md", ".txt", ".pdf", ".docx"):
        assert ext in body


# ===========================================================================
# #21. index page has question textarea (required + data-autofocus)
# ===========================================================================


async def test_index_html_has_question_textarea(app_client, auth_session):
    _user, raw = await auth_session("ta@example.com")
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/")
    app_client.cookies.clear()

    body = resp.text
    assert 'id="question"' in body
    assert "required" in body
    assert "data-autofocus" in body
    # Must be a textarea, not input.
    assert "<textarea" in body


# ===========================================================================
# #22. form wiring: no action attr (JS intercepts) + app.js loaded
# ===========================================================================


async def test_index_html_form_targets_api_research(
    app_client, auth_session
):
    _user, raw = await auth_session("wire@example.com")
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/")
    app_client.cookies.clear()

    body = resp.text
    assert 'id="research-form"' in body
    assert "/static/app.js" in body


# ===========================================================================
# #23. topbar includes logout button when authed
# ===========================================================================


async def test_topbar_logout_button_present_when_authed(
    app_client, auth_session
):
    _user, raw = await auth_session("tb@example.com")
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/")
    app_client.cookies.clear()

    body = resp.text
    assert 'id="logout-btn"' in body
    # Topbar brand + history link
    assert "Method" in body
    assert "/history" in body
