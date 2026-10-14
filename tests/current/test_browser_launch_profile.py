from pathlib import Path

from advi.capabilities.browser.executor import BrowserCapability


def test_browser_accepts_isolated_user_data_dir(tmp_path: Path):
    cap = BrowserCapability(cdp_port=9333, user_data_dir=tmp_path / "chrome-profile")
    assert cap.cdp_port == 9333
    assert cap.user_data_dir == tmp_path / "chrome-profile"
