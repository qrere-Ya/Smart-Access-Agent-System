"""shared/ui_theme.py 的純邏輯測試：token 完整性、亮暗色鍵一致、文字對比度、參數驗證（不需要顯示器）。"""
import pytest

pytest.importorskip("customtkinter")   # CI 的輕量依賴不含 customtkinter，只在本機執行
from shared import ui_theme as t  # noqa: E402


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


# --- 外觀選擇的記憶（後台右上角的淺色／深色／系統按鈕） ---

def test_the_chosen_mode_is_remembered_and_a_bad_file_falls_back(tmp_path, monkeypatch):
    from shared import paths

    monkeypatch.setattr(paths, "RUNTIME_DIR", str(tmp_path))
    assert t.load_mode() == "system"                                  # 沒有檔案
    for mode in ("light", "dark", "system"):
        t.save_mode(mode)
        assert t.load_mode() == mode
    (tmp_path / "ui_mode.json").write_text("{壞掉", encoding="utf-8")
    assert t.load_mode() == "system"
    (tmp_path / "ui_mode.json").write_text('{"mode": "rainbow"}', encoding="utf-8")
    assert t.load_mode() == "system" and t.load_mode("dark") == "dark"
    with pytest.raises(ValueError):
        t.save_mode("rainbow")


def test_every_mode_choice_has_a_label_and_a_valid_mode():
    assert [m for m, _label in t.MODE_CHOICES] == ["light", "dark", "system"]
    assert all(label for _m, label in t.MODE_CHOICES)


def test_event_only_calls_the_function_for_a_real_event_and_ignores_a_call_without_one():
    calls = []
    handler = t.event_only(lambda e: calls.append(e) or "done")
    assert handler() is None and calls == []                 # 不帶事件（tkinter 偶爾會這樣呼叫）：忽略，不出錯
    assert handler("event") == "done" and calls == ["event"]
    with_default = t.event_only(lambda _e, k="x": calls.append(k))   # 搭配預設參數的寫法也要能用
    with_default("event")
    assert calls[-1] == "x"
