"""
ingest 單元測試：各種格式的讀取（編碼、docx、無文字的 PDF）、三種切塊方式（法規、Markdown、一般）、
過長內容的切分、條號與章節標籤、檔案指紋。
"""
import zipfile

import pytest

from assistant import ingest
from assistant.ingest import Segment, chunk_document

LAW = """某某辦法
（說明文字）

## 第一章 總則

#### 第1條

為規定最低標準，特制定本辦法。

#### 第2條

本辦法用辭定義如下：

一、員工：受僱從事工作者。

二、雇主：僱用員工之人。

## 第二章 工時

#### 第3條

員工正常工作時間，每日不得超過八小時。
第一條規定於本條準用之。

#### 第3-1條

特殊行業得另行約定。
"""


def law_chunks(text=LAW, **kw):
    return chunk_document("某某辦法.txt", [Segment(text)], **kw)


# --- 法規型 ---

def test_each_article_becomes_one_chunk_with_its_chapter():
    chunks = law_chunks()
    assert [(c.section, c.article) for c in chunks if c.article] == [
        ("第一章 總則", "第1條"), ("第一章 總則", "第2條"), ("第二章 工時", "第3條"), ("第二章 工時", "第3-1條")]


def test_text_before_the_first_article_is_kept_as_a_preamble():
    first = law_chunks()[0]
    assert first.article == "" and "說明文字" in first.body


def test_a_cross_reference_at_the_start_of_a_line_is_not_an_article_heading():
    article3 = next(c for c in law_chunks() if c.article == "第3條")
    assert "第一條規定於本條準用之" in article3.body


@pytest.mark.parametrize("heading, expected", [
    ("第 12 條", "第12條"), ("#### 第三十條之一", "第三十條之一"), ("第１２條", "第１２條"),
    ("第30-1條（名稱）", "第30-1條"), ("## 第三十八條", "第三十八條"),
])
def test_article_heading_styles(heading, expected):
    text = "\n".join(f"第{i}條\n內容{i}" for i in (1, 2)) + f"\n{heading}\n這是內容。"
    assert law_chunks(text)[-1].article == expected


def test_fewer_than_three_articles_is_not_treated_as_a_law():
    chunks = law_chunks("第1條\n甲。\n第2條\n乙。")
    assert all(c.article == "" for c in chunks)


def test_label_contains_title_section_and_article_and_is_embedded_with_the_body():
    chunk = next(c for c in law_chunks() if c.article == "第2條")
    assert chunk.label == "某某辦法 第一章 總則 第2條"
    assert chunk.embed_text.startswith("某某辦法 第一章 總則 第2條\n")
    assert "二、雇主" in chunk.embed_text


def test_long_article_is_split_at_sentence_boundaries_and_numbered():
    sentences = "".join(f"這是第{i}個句子，內容很長很長很長。" for i in range(1, 30))
    text = f"第1條\n甲。\n第2條\n乙。\n第3條\n{sentences}\n第4條\n丙。"
    parts = [c for c in law_chunks(text, max_chars=100) if c.article.startswith("第3條")]
    assert len(parts) > 1
    assert parts[0].article == f"第3條（第 1/{len(parts)} 段）"
    assert all(len(c.body) <= 100 for c in parts)
    assert all(c.body.endswith("。") for c in parts)
    assert "".join(c.body.replace("\n", "") for c in parts) == sentences


def test_sentence_longer_than_the_limit_is_cut_by_length_instead_of_dropped():
    long_sentence = "字" * 250 + "。"
    text = f"第1條\n甲。\n第2條\n乙。\n第3條\n{long_sentence}"
    parts = [c for c in law_chunks(text, max_chars=100) if c.article.startswith("第3條")]
    assert all(len(c.body) <= 100 for c in parts)
    assert "".join(c.body for c in parts) == long_sentence


# --- Markdown 型 ---

MD = """# 員工手冊

## 請假辦法

### 病假

病假需附證明。

### 事假

事假須提前三天申請。

## 出勤規定

每日八點上班。
"""


def test_markdown_sections_carry_the_heading_path():
    chunks = chunk_document("手冊.md", [Segment(MD)])
    assert [(c.section, c.body) for c in chunks] == [
        ("員工手冊 > 請假辦法 > 病假", "病假需附證明。"),
        ("員工手冊 > 請假辦法 > 事假", "事假須提前三天申請。"),
        ("員工手冊 > 出勤規定", "每日八點上班。"),
    ]


def test_headings_without_body_produce_no_empty_chunks():
    assert all(c.body.strip() for c in chunk_document("手冊.md", [Segment(MD)]))


# --- 一般文件 ---

def test_plain_paragraphs_are_packed_up_to_the_limit():
    text = "\n\n".join(f"第{i}段。" + "文" * 40 for i in range(10))
    chunks = chunk_document("筆記.txt", [Segment(text)], max_chars=120)
    assert 1 < len(chunks) < 10
    assert all(len(c.body) <= 120 for c in chunks)
    assert "".join(c.body.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_page_numbers_follow_the_pdf_pages():
    segs = [Segment("第一頁的內容。", 1), Segment("\n\n第二頁的內容。", 2)]
    chunks = chunk_document("a.pdf", segs, max_chars=10)
    assert [c.page for c in chunks] == [1, 2]


def test_title_is_the_file_name_without_extension():
    assert chunk_document("sub/請假辦法.docx", [Segment("內容")])[0].title == "請假辦法"


# --- 讀檔 ---

def write(path, data):
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return str(path)


def test_utf8_text_file_with_bom(tmp_path):
    segs = ingest.read_file(write(tmp_path / "a.txt", b"\xef\xbb\xbf" + "你好".encode()))
    assert segs[0].text == "你好"


def test_big5_text_file_is_decoded(tmp_path):
    segs = ingest.read_file(write(tmp_path / "a.txt", "勞動基準法".encode("big5")))
    assert segs[0].text == "勞動基準法"


def test_undecodable_file_is_reported(tmp_path):
    with pytest.raises(ingest.DocumentReadError, match="編碼"):
        ingest.read_file(write(tmp_path / "a.txt", b"\xff\xfe\x00\xd8\xff"))


def test_empty_file_is_reported(tmp_path):
    with pytest.raises(ingest.DocumentReadError, match="沒有可擷取"):
        ingest.read_file(write(tmp_path / "a.md", "  \n\n"))


def test_unsupported_extension_is_reported(tmp_path):
    with pytest.raises(ingest.DocumentReadError, match="不支援"):
        ingest.read_file(write(tmp_path / "a.xlsx", b"x"))


def make_docx(path, paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument'
                   '.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml",
                   '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                   f'2006/main"><w:body>{body}</w:body></w:document>')
    return str(path)


def test_docx_text_is_extracted(tmp_path):
    segs = ingest.read_file(make_docx(tmp_path / "a.docx", ["特休規定", "滿一年給七天"]))
    assert "特休規定" in segs[0].text and "滿一年給七天" in segs[0].text


def test_corrupt_docx_is_reported_not_raised_raw(tmp_path):
    with pytest.raises(ingest.DocumentReadError):
        ingest.read_file(write(tmp_path / "a.docx", b"not a zip"))


def test_pdf_without_text_is_reported(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    path = tmp_path / "scan.pdf"
    with open(path, "wb") as fh:
        writer.write(fh)
    with pytest.raises(ingest.DocumentReadError):
        ingest.read_file(str(path))


def test_fingerprint_changes_with_content_only(tmp_path):
    a = write(tmp_path / "a.txt", "內容一")
    first = ingest.file_fingerprint(a)
    assert ingest.file_fingerprint(a) == first
    write(tmp_path / "a.txt", "內容二")
    assert ingest.file_fingerprint(a) != first
