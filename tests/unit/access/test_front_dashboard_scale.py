"""前台儀表板的縮放倍率：以 1920×1080 為 1.0，依視窗大小縮放、扣掉系統 DPI、限制範圍。"""
import pytest

pytest.importorskip("customtkinter")
from access import front_dashboard as fd  # noqa: E402


def test_window_scale_is_one_at_the_design_size_and_follows_the_smaller_side():
    assert fd.window_scale(1920, 1080) == 1.0
    assert fd.window_scale(1280, 1080) == pytest.approx(0.65, abs=0.05)         # 寬度較小以寬度為準
    assert fd.window_scale(1920, 540) == 0.5


def test_window_scale_removes_the_system_dpi_and_stays_in_range():
    assert fd.window_scale(1920, 1080, dpi=1.25) == 0.8
    assert fd.window_scale(100, 100) == fd.MIN_SCALE
    assert fd.window_scale(9000, 9000) == fd.MAX_SCALE
    assert fd.window_scale(1920, 1080, dpi=0) == 1.0
