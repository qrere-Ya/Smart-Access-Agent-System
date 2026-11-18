"""svg_icon 的單元測試：專案圖示都畫得出來、顏色與大小正確、壞檔與危險名稱不會當掉、不支援的內容回傳 None。"""
import glob
import os

import pytest

from shared import paths, svg_icon


def icon_names():
    return sorted(os.path.basename(f)[:-4] for f in glob.glob(os.path.join(paths.ICONS_DIR, "*.svg")))


def test_every_project_icon_renders_with_pixels_in_the_requested_color():
    names = icon_names()
    assert len(names) >= 12
    for name in names:
        image = svg_icon.render(name, 48, "#ff0000")
        assert image is not None and image.size == (48, 48), name
        pixels = [p for p in image.getdata() if p[3] > 200]
        assert len(pixels) > 40, name                                     # 真的有畫出線條
        assert all(p[:3] == (255, 0, 0) for p in pixels), name           # 顏色一致（currentColor 換成指定色）


def test_unsafe_or_missing_names_return_none_and_do_not_read_outside_the_folder():
    for bad in ("../shared/paths", "a/b", "..\\x", "", "x" * 41, "no_such_icon"):
        assert svg_icon.render(bad, 16, "#000000") is None


@pytest.fixture
def icon_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ICONS_DIR", str(tmp_path))
    svg_icon._cache.clear()
    yield tmp_path
    svg_icon._cache.clear()


def write(folder, name, body):
    (folder / f"{name}.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="2">{body}</svg>', encoding="utf-8")


def test_a_filled_shape_and_a_line_are_drawn(icon_dir):
    write(icon_dir, "box", '<rect x="4" y="4" width="16" height="16" fill="currentColor"/>')
    image = svg_icon.render("box", 24, "#00ff00")
    assert image.getpixel((12, 12))[3] == 255 and image.getpixel((1, 1))[3] == 0
    write(icon_dir, "bar", '<path d="M4 12 H20"/>')
    assert svg_icon.render("bar", 24, "#00ff00").getpixel((12, 12))[3] > 200


def test_unsupported_elements_and_path_commands_return_none(icon_dir):
    write(icon_dir, "text", '<text x="1" y="1">x</text>')
    write(icon_dir, "arc", '<path d="M4 4 A5 5 0 0 1 10 10"/>')
    write(icon_dir, "short", '<path d="M4"/>')
    for name in ("text", "arc", "short"):
        assert svg_icon.render(name, 24, "#000000") is None


def test_doctype_entity_and_broken_files_are_refused(icon_dir):
    (icon_dir / "evil.svg").write_text('<!DOCTYPE svg [<!ENTITY a "aaa">]><svg viewBox="0 0 24 24"/>', encoding="utf-8")
    (icon_dir / "broken.svg").write_text("<svg", encoding="utf-8")
    (icon_dir / "badbox.svg").write_text('<svg viewBox="0 0 0 0"/>', encoding="utf-8")
    for name in ("evil", "broken", "badbox"):
        assert svg_icon.render(name, 24, "#000000") is None


def test_two_color_icons_use_both_colors_and_semi_transparent_fills():
    image = svg_icon.render("smart_chat", 64, "#ffffff", "#ff0000")
    colors = {p[:3] for p in image.getdata() if p[3] > 200}
    assert (255, 255, 255) in colors and (255, 0, 0) in colors               # 主色與強調色都有
    soft = [p for p in image.getdata() if 20 < p[3] < 120 and p[:3] == (255, 0, 0)]
    assert soft                                                              # fill-opacity 的半透明填色
    mono = svg_icon.render("smart_chat", 64, "#00ff00")
    assert {p[:3] for p in mono.getdata() if p[3] > 200} == {(0, 255, 0)}     # 沒給 accent 就是單色


def test_glow_makes_a_bigger_image_with_a_soft_halo():
    plain = svg_icon.render("smart_access", 40, "#ffffff", "#22d3ee")
    glowing = svg_icon.render("smart_access", 40, "#ffffff", "#22d3ee", glow=True)
    assert glowing.size == (60, 60) and plain.size == (40, 40)
    assert glowing.getpixel((1, 1))[3] == 0 or glowing.getpixel((8, 30))[3] > 0
