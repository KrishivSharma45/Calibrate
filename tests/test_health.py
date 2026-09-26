from fastapi.testclient import TestClient

from app.main import app


def test_healthz_reports_ok():
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_pico_css_is_served_locally():
    resp = TestClient(app).get("/static/pico.min.css")
    assert resp.status_code == 200
    assert "Pico CSS" in resp.text
