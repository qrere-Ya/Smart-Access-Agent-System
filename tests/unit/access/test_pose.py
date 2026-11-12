"""多人入鏡：只處理離鏡頭最近的臉與人（臉框面積最大），遠處背景的人不算。"""
from access import pose


def face(x1, y1, x2, y2):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def test_nearest_face_is_the_largest_box_and_none_when_empty():
    far, near = face(0, 0, 50, 50), face(100, 100, 300, 320)
    assert pose.nearest_face([far, near]) is near and pose.nearest_face([near, far]) is near
    assert pose.nearest_face([]) is None and pose.nearest_face([far]) is far


def test_near_person_boxes_drop_small_background_people_but_keep_someone_right_behind():
    front = (100, 50, 500, 700)                      # 最近的人
    behind = (300, 80, 640, 700)                     # 緊跟在後面的人，約 0.8 倍
    passerby = (0, 300, 120, 600)                    # 遠處路人，很小
    kept = pose.near_person_boxes([front, behind, passerby])
    assert front in kept and behind in kept and passerby not in kept
    assert pose.near_person_boxes([]) == [] and pose.near_person_boxes([passerby]) == [passerby]


def test_box_area_never_goes_negative_for_broken_boxes():
    assert pose.box_area((10, 10, 5, 5)) == 0.0
