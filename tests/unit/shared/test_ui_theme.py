"""shared/ui_theme.py 的純邏輯測試：token 完整性、亮暗色鍵一致、文字對比度、參數驗證（不需要顯示器）。"""
import pytest

from shared import ui_theme as t


def test_light_and_dark_have_same_keys():
    assert set(t.COLORS["light"]) == set(t.COLORS["dark"])


def test_all_colors_are_hex():
    for mode in t.MODES:
        for name, value in t.COLORS[mode].items():
            assert len(value) == 7 and value[0] == "#", (mode, name)
            int(value[1:], 16)


def test_required_tokens_present():
    for name in ("primary", "danger", "warning", "success", "secondary", "bg", "card", "border", "text"):
        assert name in t.COLORS["light"]


def test_pair_returns_light_dark():
    assert t.pair("primary") == (t.COLORS["light"]["primary"], t.COLORS["dark"]["primary"])


def test_contrast_ratio_extremes():
    assert t.contrast_ratio("#000000", "#ffffff") == pytest.approx(21, rel=1e-3)
    assert t.contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1)


@pytest.mark.parametrize("mode", t.MODES)
def test_text_contrast_is_readable(mode):
    c = t.COLORS[mode]
    checks = [("text", "bg"), ("text", "card"), ("text", "table_bg"), ("text", "table_alt"), ("text", "table_head"),
              ("text", "table_select"), ("text_muted", "bg"), ("text_muted", "card"),
              ("on_primary", "primary"), ("on_primary", "primary_hover"), ("on_danger", "danger"),
              ("on_danger", "danger_hover"), ("on_secondary", "secondary"), ("on_secondary", "secondary_hover")]
    checks += [(fg, bg) for bg, fg in t.BADGE_STYLES.values()]
    for fg, bg in checks:
        assert t.contrast_ratio(c[fg], c[bg]) >= 4.5, (mode, fg, bg, t.contrast_ratio(c[fg], c[bg]))


def test_badge_styles_reference_existing_tokens():
    for bg, fg in t.BADGE_STYLES.values():
        assert bg in t.COLORS["light"] and fg in t.COLORS["light"]


def test_apply_theme_rejects_unknown_mode():
    with pytest.raises(ValueError):
        t.apply_theme("sepia")


def test_zebra_tags_alternate():
    assert t.zebra_tags(0) != t.zebra_tags(1)
