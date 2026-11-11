"""前台新聞 QR Code：矩陣是正方形的 0／1、圖片邊長正確、空白或太長的網址不做。"""
import pytest

pytest.importorskip("pyqrcode")
from access import front_qr  # noqa: E402


def test_matrix_is_square_binary_and_encodes_the_url():
    code = front_qr.matrix("https://example.com/news/1")
    assert code and all(len(row) == len(code) for row in code)
    assert {bit for row in code for bit in row} <= {0, 1}
    assert front_qr.matrix("https://example.com/news/2") != code


def test_blank_or_too_long_urls_give_nothing():
    assert front_qr.matrix("") is None and front_qr.matrix(None) is None
    assert front_qr.matrix("https://example.com/" + "x" * front_qr.MAX_URL) is None


def test_image_has_the_requested_size_and_is_black_and_white():
    img = front_qr.image("https://example.com/n/1", size=150)
    assert img.size == (150, 150)
    colours = set(img.getdata())
    assert colours <= {(0, 0, 0), (255, 255, 255)} and len(colours) == 2
    assert front_qr.image("", size=150) is None
