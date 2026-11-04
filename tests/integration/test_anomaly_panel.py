"""
異常事件視窗的真 Tk 測試：列表與未讀標記、選取後預覽證據照片、沒有照片的提示、標為已讀同步通知列、
通知列與彈窗的「查看」按鈕。並截圖存到 runtime/screenshots/。
"""
import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def evidence_photo():
    """在（測試用的）證據資料夾放一張真的圖片，回傳路徑。"""
    from PIL import Image

    from shared import paths

    os.makedirs(paths.ANOMALY_DIR, exist_ok=True)
    path = os.path.join(paths.ANOMALY_DIR, "tailgate_test.jpg")
    Image.new("RGB", (540, 1120), (90, 120, 160)).save(path)
    yield path
    os.remove(path)


@pytest.fixture
def notifier(tmp_path):
    from admin import anomaly_notifier

    return anomaly_notifier.AnomalyNotifier(str(tmp_path / "seen.json"))


def _rows(panel):
    return [panel.tree.item(i, "values") for i in panel.tree.get_children()]


def test_list_shows_unread_marks_and_chinese_types(root, db, notifier, evidence_photo):
    from admin import anomaly_panel
    from shared import database_mgr as dbm

    notifier.poll()                                          # 先建立已讀基準（沒有事件）
    dbm.add_anomaly_event("tailgate", evidence_photo, "畫面有 2 人，只辨識到 1 張臉")
    dbm.add_anomaly_event("no_scan_walkby", None, "")
    panel = anomaly_panel.AnomalyPanel(root, notifier)
    panel.pack()
    root.update()
    rows = _rows(panel)
    assert len(rows) == 2
    assert {r[2] for r in rows} == {"尾隨", "經過鏡頭未打卡"} and all(r[0] == "● 未讀" for r in rows)
    assert "未讀 2 筆" in panel.summary.cget("text")
    panel.destroy()


def test_selecting_an_event_previews_its_photo_or_explains_there_is_none(root, db, notifier, evidence_photo):
    from admin import anomaly_panel
    from shared import database_mgr as dbm

    dbm.add_anomaly_event("tailgate", evidence_photo, "有照片")
    dbm.add_anomaly_event("no_scan_walkby", None, "沒照片")
    panel = anomaly_panel.AnomalyPanel(root, notifier)
    panel.pack()
    root.update()
    items = list(panel.tree.get_children())
    with_photo = [i for i in items if panel.tree.item(i, "values")[5] == "有"]
    without_photo = [i for i in items if panel.tree.item(i, "values")[5] == "無"]
    panel.tree.selection_set(with_photo[0])
    root.update()
    assert panel._preview_image is not None and "尾隨" in panel.detail.cget("text")
    panel.tree.selection_set(without_photo[0])
    root.update()
    assert panel._preview_image is None and "沒有證據照片" in panel.preview.cget("text")
    panel.destroy()


def test_an_evidence_path_outside_the_folder_is_never_loaded(root, db, notifier, tmp_path):
    from admin import anomaly_panel
    from shared import database_mgr as dbm

    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"not an image")
    dbm.add_anomaly_event("tailgate", str(outside), "路徑被竄改")
    panel = anomaly_panel.AnomalyPanel(root, notifier)
    panel.pack()
    root.update()
    panel.tree.selection_set(panel.tree.get_children()[0])
    root.update()
    assert panel._preview_image is None and "沒有證據照片" in panel.preview.cget("text")
    panel.destroy()


def test_mark_all_read_updates_the_list_and_calls_back(root, db, notifier):
    from admin import anomaly_panel
    from shared import database_mgr as dbm

    notifier.poll()
    dbm.add_anomaly_event("tailgate", None, "事件")
    notifier.poll()
    called = []

    def mark_read():
        called.append(1)
        notifier.mark_all_read()

    panel = anomaly_panel.AnomalyPanel(root, notifier, on_mark_read=mark_read)
    panel.pack()
    root.update()
    assert "未讀 1 筆" in panel.summary.cget("text")
    panel.mark_all_read()
    assert called == [1] and "未讀 0 筆" in panel.summary.cget("text")
    assert _rows(panel)[0][0] == "已讀"
    panel.destroy()


def _texts(widget):
    """遞迴收集某個元件底下所有有文字的元件的文字。"""
    found = []
    for child in widget.winfo_children():
        try:
            found.append(child.cget("text"))
        except Exception:  # noqa: S110  不是有文字的元件
            pass
        found.extend(_texts(child))
    return found


def test_the_notification_bar_and_popup_can_open_the_list(root, db, notifier):
    from admin import notification_bar
    from shared import database_mgr as dbm

    opened = []
    bar = notification_bar.NotificationBar(root, notifier=notifier, poll_ms=60000, on_open=lambda: opened.append(1))
    bar.frame.pack()
    assert "查看" in _texts(bar.frame)
    dbm.add_anomaly_event("tailgate", None, "新事件")
    bar.refresh()
    root.update()
    bar.open_list()
    assert opened == [1]
    assert bar._popup is not None and bar._popup.winfo_exists() and "查看" in _texts(bar._popup)
    bar.stop()


def test_the_notification_bar_without_a_handler_has_no_view_button(root, db, notifier):
    from admin import notification_bar

    bar = notification_bar.NotificationBar(root, notifier=notifier, poll_ms=60000)
    bar.frame.pack()
    assert "查看" not in _texts(bar.frame)
    bar.open_list()                                          # 沒有提供 on_open 時什麼都不做，也不出錯
    bar.stop()


def test_anomaly_window_screenshot(root, db, notifier, evidence_photo, screenshot, appearance):
    from admin import anomaly_panel
    from shared import database_mgr as dbm

    notifier.poll()
    dbm.add_anomaly_event("tailgate", evidence_photo, "畫面有 2 人，只辨識到 1 張臉（連續 5 次，20:21:19 ~ 20:21:20）")
    dbm.add_anomaly_event("no_scan_walkby", None, "")
    dbm.add_anomaly_event("login_failed", None, "登入失敗")
    for mode in ("light", "dark"):
        appearance(mode)
        win = anomaly_panel.open_anomaly_window(root, notifier)
        root.update()
        panel = win.winfo_children()[0]
        panel.tree.selection_set(panel.tree.get_children()[2])
        root.update()
        assert screenshot(win, f"後台_異常事件_{mode}")
        win.destroy()
