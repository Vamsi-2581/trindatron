import http.server
import threading

import pytest

from trinity_agent.tools.actions import ScheduleAction, SendNotification
from trinity_agent.tools.base import SideEffect
from trinity_agent.tools.documents import CreateDocument
from trinity_agent.tools.registry import ToolRegistry
from trinity_agent.tools.web import BrowseUrl, UnsafeUrlError, assert_url_is_safe_to_fetch


# ─── CreateDocument ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_document_success():
    tool = CreateDocument()
    result = await tool.run(title="My Doc", content="hello")
    assert result.success
    assert "My Doc" in result.output
    assert "hello" in result.output
    assert tool.side_effect == SideEffect.READ_ONLY
    assert tool.requires_approval is False


# ─── SendNotification / ScheduleAction ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_notification_is_approval_gated_stub():
    tool = SendNotification()
    assert tool.requires_approval is True
    assert tool.side_effect == SideEffect.EXTERNAL_SIDE_EFFECT
    result = await tool.run(destination="user@example.com", message="hi")
    assert result.success
    assert "send_notification" in result.output


@pytest.mark.asyncio
async def test_schedule_action_is_approval_gated_stub():
    tool = ScheduleAction()
    assert tool.requires_approval is True
    assert tool.side_effect == SideEffect.EXTERNAL_SIDE_EFFECT
    result = await tool.run(action="do the thing", when="tomorrow")
    assert result.success
    assert "schedule_action" in result.output


def test_registry_reports_approval_and_side_effect_metadata():
    registry = ToolRegistry()
    registry.register(SendNotification())
    registry.register(CreateDocument())
    send = registry.get("send_notification")
    doc = registry.get("create_document")
    assert send.requires_approval is True
    assert doc.requires_approval is False


# ─── BrowseUrl — SSRF hardening (canonical location) ────────────────────────────

@pytest.fixture()
def marker_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"internal-secret-marker")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


@pytest.mark.asyncio
async def test_browse_url_rejects_127_0_0_1(marker_server):
    tool = BrowseUrl()
    result = await tool.run(url=f"http://127.0.0.1:{marker_server}/secret")
    assert not result.success
    assert "internal-secret-marker" not in result.error


@pytest.mark.asyncio
async def test_browse_url_rejects_localhost_hostname(marker_server):
    tool = BrowseUrl()
    result = await tool.run(url=f"http://localhost:{marker_server}/secret")
    assert not result.success


@pytest.mark.asyncio
async def test_browse_url_rejects_decimal_ip_form_of_127_0_0_1(marker_server):
    # 2130706433 == 127.0.0.1 in decimal form
    result = await BrowseUrl().run(url=f"http://2130706433:{marker_server}/secret")
    assert not result.success


@pytest.mark.asyncio
async def test_assert_url_is_safe_to_fetch_rejects_metadata_endpoint():
    with pytest.raises(UnsafeUrlError):
        await assert_url_is_safe_to_fetch("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_assert_url_is_safe_to_fetch_allows_a_real_public_style_host():
    """Regression guard: the SSRF fix must not reject legitimate public hosts."""
    await assert_url_is_safe_to_fetch("http://8.8.8.8/")


def test_browse_url_is_read_only_and_never_requires_approval():
    tool = BrowseUrl()
    assert tool.side_effect == SideEffect.READ_ONLY
    assert tool.requires_approval is False
