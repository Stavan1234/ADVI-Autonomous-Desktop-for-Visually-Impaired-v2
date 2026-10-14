from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.desktop.context import DesktopContext


def test_browser_refuses_unowned_existing_tab_when_dedicated_creation_fails():
    cap = BrowserCapability(context=DesktopContext(), cdp_port=9333)

    class FakeCDP:
        def list_tabs(self):
            return [
                {
                    "id": "user-tab",
                    "type": "page",
                    "url": "https://private.example/",
                    "title": "User tab",
                }
            ]

        def create_new_tab(self, url):
            assert url == "about:blank"
            return None

    cap.cdp = FakeCDP()

    assert cap._ensure_chrome_running() is False
    assert cap.context.target_tab_id is None
