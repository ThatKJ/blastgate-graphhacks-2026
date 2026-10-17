"""Server-side protection of the only route that can spend provider quota: /api/agent/investigate in live
(groq) mode. Offline: the provider call is stubbed, so reaching it shows up as a 502 from the stub."""
import pytest
from fastapi.testclient import TestClient

from blastgate import agent as AG
from blastgate.api import Settings, create_app

LOCAL, REMOTE = ("127.0.0.1", 50000), ("203.0.113.7", 50000)   # TEST-NET-3 documentation address


@pytest.fixture
def live(monkeypatch):
    """Factory for a live-mode app whose investigate() is a stub that counts calls."""
    calls = []

    async def stub(*a, **k):
        calls.append(1)
        raise AG.ProviderError("provider_error", "stub provider reached")

    def make(client=LOCAL, **env):
        for k in ("AGENT_LIVE_TOKEN", "AGENT_LIVE_MAX_PER_CLIENT_MINUTE", "AGENT_LIVE_MAX_PER_MINUTE",
                  "AGENT_LIVE_MAX_PER_DAY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AGENT_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_real")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(AG, "investigate", stub)
        app = create_app()
        return app, (lambda c=client: TestClient(app, raise_server_exceptions=False, client=c))
    make.calls = calls
    return make


def post(c, headers=None):
    return c.post("/api/agent/investigate", json={"pr_id": "PR-104"}, headers=headers or {})


def test_without_token_only_this_machine_may_go_live(live):
    _, tc = live()
    assert post(tc(LOCAL)).status_code == 502                      # reached the (stub) provider
    r = post(tc(REMOTE))
    assert r.status_code == 403 and r.json()["error"] == "live_access_local_only"
    for hdr in ({"X-Forwarded-For": "198.51.100.1"}, {"Forwarded": "for=198.51.100.1"}):
        assert post(tc(LOCAL), hdr).json()["error"] == "live_access_local_only"   # proxied => maybe remote
    assert len(live.calls) == 1


def test_token_mode_requires_the_exact_bearer_token(live):
    _, tc = live(AGENT_LIVE_TOKEN="s3cret-live-token")
    for headers in (None, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret-live-token"}):
        r = post(tc(REMOTE), headers)
        assert r.status_code == 401 and r.json()["error"] == "live_access_denied"
        assert "s3cret-live-token" not in r.text
    assert post(tc(LOCAL)).status_code == 401                       # the token applies to localhost too
    assert post(tc(REMOTE), {"Authorization": "Bearer s3cret-live-token"}).status_code == 502
    assert len(live.calls) == 1


def test_rate_limits_are_enforced_server_side(live):
    _, tc = live(AGENT_LIVE_TOKEN="t", AGENT_LIVE_MAX_PER_CLIENT_MINUTE="2", AGENT_LIVE_MAX_PER_MINUTE="3")
    ok = {"Authorization": "Bearer t"}
    a, b, c = tc(("198.51.100.1", 1)), tc(("198.51.100.2", 1)), tc(("198.51.100.3", 1))
    assert [post(a, ok).status_code for _ in range(2)] == [502, 502]
    r = post(a, ok)                                                 # per-client cap
    assert r.status_code == 429 and r.json()["error"] == "live_rate_limited"
    assert int(r.headers["retry-after"]) > 0 and r.json()["retry_after"] == r.headers["retry-after"]
    assert post(b, ok).status_code == 502                           # 3rd allowed call overall
    assert post(c, ok).status_code == 429                           # global per-minute cap
    assert len(live.calls) == 3


def test_refused_callers_do_not_consume_the_budget(live):
    _, tc = live(AGENT_LIVE_TOKEN="t", AGENT_LIVE_MAX_PER_MINUTE="1")
    for _ in range(5):
        assert post(tc(REMOTE)).status_code == 401
    assert post(tc(REMOTE), {"Authorization": "Bearer t"}).status_code == 502


def test_daily_cap(live):
    _, tc = live(AGENT_LIVE_MAX_PER_DAY="1", AGENT_LIVE_MAX_PER_CLIENT_MINUTE="5", AGENT_LIVE_MAX_PER_MINUTE="5")
    assert post(tc()).status_code == 502
    r = post(tc())
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 3600


def test_demo_and_deterministic_routes_are_not_gated(monkeypatch):
    for k in ("GROQ_API_KEY", "AGENT_LIVE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENT_PROVIDER", "deterministic")
    calls = []

    async def stub(*a, **k):
        calls.append(1)
        raise AG.ProviderError("provider_error", "stub")

    monkeypatch.setattr(AG, "investigate", stub)
    c = TestClient(create_app(), raise_server_exceptions=False, client=REMOTE)
    assert post(c).status_code == 502 and calls == [1]              # no provider spend in demo mode
    assert c.post("/api/analyze", json={"pr_id": "PR-999"}).status_code == 404   # deterministic route reachable


def test_health_reports_the_gate_never_the_token(live, monkeypatch):
    _, tc = live()
    h = tc(REMOTE).get("/health").json()["agent_provider"]
    assert h["live_access"] == "localhost_only"
    _, tc = live(AGENT_LIVE_TOKEN="s3cret-live-token")
    r = tc(REMOTE).get("/health")
    assert r.json()["agent_provider"]["live_access"] == "token" and "s3cret-live-token" not in r.text
    monkeypatch.setenv("AGENT_PROVIDER", "deterministic")
    assert Settings.from_env().live_access is None
