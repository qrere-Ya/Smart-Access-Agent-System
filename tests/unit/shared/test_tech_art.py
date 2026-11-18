"""tech_art 的單元測試：卡片底圖、徽章與漸層線都畫得出來、尺寸正確、每個模組色在亮暗兩套都存在。"""
import pytest

from shared import tech_art, ui_theme

MODULE_TOKENS = [k for k in ui_theme.COLORS["dark"] if k.startswith("mod_")]


def test_every_module_has_its_own_distinct_color_in_both_modes():
    assert len(MODULE_TOKENS) == 11
    for mode in ui_theme.MODES:
        values = [ui_theme.COLORS[mode][k] for k in MODULE_TOKENS]
        assert len(set(values)) == len(values), mode


@pytest.mark.parametrize("mode", ui_theme.MODES)
def test_the_card_frame_has_the_requested_size_and_corner_brackets(mode):
    accent = ui_theme.COLORS[mode]["mod_chat"]
    image = tech_art._frame_image(mode, 200, 120, accent, False)
    assert image.size == (200, 120)
    assert image.getpixel((100, 60))[:3] == tuple(int(ui_theme.COLORS[mode]["card"][i:i + 2], 16) for i in (1, 3, 5))
    corner = [image.getpixel((x, 8 + 1))[:3] for x in range(8, 30)]
    want = tuple(int(accent[i:i + 2], 16) for i in (1, 3, 5))
    near = [all(abs(x - y) <= 6 for x, y in zip(c, want, strict=True)) for c in corner]
    assert any(near)                                  # 左上角括號是模組色（邊緣有抗鋸齒）
    hover = tech_art._frame_image(mode, 200, 120, accent, True)
    assert hover.getpixel((100, 0)) != image.getpixel((100, 0))                # hover 邊線改成全亮的模組色


@pytest.mark.parametrize("mode", ui_theme.MODES)
def test_the_badge_and_the_line_render(mode):
    badge = tech_art._badge_image(mode, "smart_rag", 60, ui_theme.COLORS[mode]["mod_rag"])
    assert badge is not None and badge.size == (60 + 2 * (60 // 5),) * 2
    line = tech_art._line_image(mode, 300, 4, "glow", "accent")
    assert line.size == (300, 4) and line.getpixel((0, 2))[3] < line.getpixel((150, 2))[3]   # 兩端淡出


def test_an_unknown_icon_gives_none_instead_of_crashing():
    assert tech_art._badge_image("dark", "no_such_icon", 40, "#ffffff") is None
