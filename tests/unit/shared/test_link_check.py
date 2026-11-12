"""外部網址檢查（後台晨報連結與前台 QR 共用）：只收 https 公開網域。"""
import pytest

from shared import link_check


@pytest.mark.parametrize("url, ok", [
    ("https://www.ithome.com.tw/news/1", True),
    ("https://example.com/", True),
    ("http://example.com/", False),                 # 不是 https
    ("https://127.0.0.1/x", False),                 # IP
    ("https://[::1]/x", False),
    ("https://localhost/x", False),
    ("https://nas.local/x", False),                 # 內網後綴
    ("https://intranet/x", False),                  # 沒有點
    ("https://user:pw@example.com/", False),        # 帶帳號密碼
    ("", False), (None, False), ("not a url", False),
    ("https://exa mple.com/", False),
])
def test_only_public_https_urls_pass(url, ok):
    assert (link_check.link_problem(url) == "") is ok


def test_news_feed_keeps_the_old_name_for_existing_callers():
    from admin import news_feed

    assert news_feed.link_problem is link_check.link_problem
