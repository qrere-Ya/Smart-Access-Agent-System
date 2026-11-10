"""
尾隨偵測的人數計算：用真的 YOLO 與兩張內建的單人照片（scikit-image 附的公有領域照片）合成畫面。
單人要算 1 人（不能因為框兩次誤報尾隨），兩人並排或貼近重疊也要算 2 人（去重不能把兩個人合併）。
"""
import cv2
import numpy as np
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_models")]

HEIGHT = 480


def _portrait(image, gray=False):
    if gray:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    return cv2.resize(image, (int(image.shape[1] * HEIGHT / image.shape[0]), HEIGHT))


@pytest.fixture(scope="module")
def people():
    from skimage import data

    return _portrait(data.astronaut()), _portrait(data.camera(), gray=True)


def _scene(*placements):
    canvas = np.full((HEIGHT, 1280, 3), 127, np.uint8)
    for image, x in placements:
        width = min(image.shape[1], canvas.shape[1] - x)
        canvas[:, x:x + width] = image[:, :width]
    return canvas


@pytest.fixture(autouse=True)
def _release_yolo():
    yield
    from access import security

    security.release_yolo()


def test_one_person_is_counted_once(people):
    from access import security

    for person in people:
        assert security.count_persons(_scene((person, 300)))[0] == 1


@pytest.mark.parametrize("overlap", [0.0, 0.2, 0.4])
def test_two_people_side_by_side_or_overlapping_are_counted_twice(people, overlap):
    from access import security

    first, second = people
    x = int(first.shape[1] * (1 - overlap)) + (20 if overlap == 0 else 0)
    assert security.count_persons(_scene((first, 0), (second, x)))[0] == 2
