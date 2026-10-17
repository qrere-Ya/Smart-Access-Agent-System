"""
ingest.py：文件讀取與切塊（助理知識庫的第一步）。

主要功能：
  - 讀取 .txt、.md、.pdf、.docx（PDF 與 Word 用 llama-index 的 reader；文字檔自動判斷編碼）
  - 依文件本身的結構切塊，每塊都帶上「文件名、章節、條號、頁碼」，單獨拿出來也看得懂出處：
      法規型（出現 3 個以上「第 N 條」標題）→ 一條一塊；
      Markdown 型（有標題）→ 依標題分節；
      其他 → 依段落裝填到字數上限
  - 太長的單元再依段落、句子切開，不會從句子中間斷開

串接模組：
  - doc_store：把切好的塊向量化並存入向量庫
"""
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

SUPPORTED_EXTENSIONS = (".txt", ".md", ".pdf", ".docx")
DEFAULT_MAX_CHARS = 600
_TEXT_ENCODINGS = ("utf-8-sig", "big5", "gb18030")

_NUM = r"[0-9０-９一二三四五六七八九十百千零]+"
# 條號標題獨占一行：「第 1 條」「#### 第30-1條」「第三十條之一（名稱）」；行首的交叉引用（後面接正文）不算
_ARTICLE_LINE = re.compile(
    rf"^\s*(?:#{{1,6}}\s*)?第\s*(?P<no>{_NUM}(?:\s*[-－]\s*{_NUM})?)\s*條(?P<sub>\s*之\s*{_NUM})?"
    r"\s*(?:[（(][^）)]*[）)])?\s*$")
_CHAPTER_LINE = re.compile(rf"^\s*(?:#{{1,6}}\s*)?第\s*{_NUM}\s*[編章節款]\s*[^。]{{0,40}}$")
_MD_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])")
_LEGAL_MIN_ARTICLES = 3


class DocumentReadError(Exception):
    """文件讀不出內容（損毀、加密、掃描檔沒有文字、編碼無法判斷）。"""


@dataclass
class Segment:
    """讀出來的一段文字；PDF 以頁為單位，其他格式 page 為 None。"""
    text: str
    page: Optional[int] = None


@dataclass
class Chunk:
    """切好的一塊：body 是正文，label 是出處標籤；向量化時兩者一起送。"""
    source: str
    title: str
    section: str
    article: str
    page: Optional[int]
    body: str

    @property
    def label(self) -> str:
        return " ".join(part for part in (self.title, self.section, self.article) if part)

    @property
    def embed_text(self) -> str:
        return f"{self.label}\n{self.body}"


def file_fingerprint(path: str) -> str:
    """檔案內容的 SHA256，用來判斷文件有沒有變動。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode(raw: bytes) -> str:
    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentReadError("無法判斷文字編碼（已試 UTF-8、Big5、GB18030）")


def _page_number(metadata, fallback):
    label = (metadata or {}).get("page_label")
    return int(label) if str(label).isdigit() else fallback


def read_file(path: str) -> List[Segment]:
    """讀取文件，回傳文字段落清單；讀不到任何文字時拋出 DocumentReadError。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".txt", ".md"):
            with open(path, "rb") as fh:
                segments = [Segment(_decode(fh.read()))]
        elif ext == ".pdf":
            from llama_index.readers.file import PDFReader
            docs = PDFReader().load_data(Path(path))
            segments = [Segment(d.text, _page_number(d.metadata, i)) for i, d in enumerate(docs, start=1)]
        elif ext == ".docx":
            from llama_index.readers.file import DocxReader
            segments = [Segment(d.text) for d in DocxReader().load_data(Path(path))]
        else:
            raise DocumentReadError(f"不支援的檔案類型：{ext}")
    except DocumentReadError:
        raise
    except Exception as e:
        raise DocumentReadError(f"{type(e).__name__}: {e}") from e
    segments = [s for s in segments if s.text and s.text.strip()]
    if not segments:
        raise DocumentReadError("文件裡沒有可擷取的文字（可能是掃描檔或空白檔）")
    return segments


def _lines_with_page(segments: List[Segment]):
    lines = []
    for seg in segments:
        for line in seg.text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            lines.append((seg.page, line.rstrip()))
    return lines


def _clean_title(text: str) -> str:
    return re.sub(r"^\s*#{1,6}\s*", "", text).strip()


def _split_long(body: str, max_chars: int) -> List[str]:
    """把過長的正文依段落、句子切成不超過 max_chars 的幾份；單一句子仍太長才硬切。"""
    if len(body) <= max_chars:
        return [body]
    pieces = []
    for paragraph in re.split(r"\n+", body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            while len(sentence) > max_chars:
                pieces.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            if sentence.strip():
                pieces.append(sentence)
    parts, current = [], ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > max_chars:
            parts.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts


def _emit(chunks, source, title, section, article, page, body, max_chars):
    """把一個單元（一條、一節、一組段落）加入結果；太長就切成多塊並在標籤標示第幾段。"""
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body:
        return
    parts = _split_long(body, max_chars)
    for number, part in enumerate(parts, start=1):
        marker = f"（第 {number}/{len(parts)} 段）" if len(parts) > 1 else ""
        chunks.append(Chunk(source, title, section, f"{article}{marker}" if article else marker, page, part))


def _chunk_legal(source, title, lines, max_chars):
    """法規型：一條一塊，章節標題記在 section。"""
    chunks, section, article, page, buffer = [], "", "", None, []

    def flush():
        _emit(chunks, source, title, section, article, page, "\n".join(buffer), max_chars)
        buffer.clear()

    for line_page, line in lines:
        m = _ARTICLE_LINE.match(line)
        if m:
            flush()
            number = re.sub(r"\s+", "", m["no"])
            suffix = re.sub(r"\s+", "", m["sub"] or "")
            article, page = f"第{number}條{suffix}", line_page
        elif _CHAPTER_LINE.match(line) or _MD_HEADING.match(line):
            flush()
            section, article, page = _clean_title(line), "", line_page
        else:
            if not buffer and not article:
                page = line_page
            buffer.append(line)
    flush()
    return chunks


def _chunk_markdown(source, title, lines, max_chars):
    """Markdown 型：依標題分節，section 記標題路徑（一級 > 二級 > …）。"""
    chunks, stack, buffer, page = [], [], [], None

    def flush():
        _emit(chunks, source, title, " > ".join(t for _, t in stack), "", page, "\n".join(buffer), max_chars)
        buffer.clear()

    for line_page, line in lines:
        m = _MD_HEADING.match(line)
        if m:
            flush()
            level = len(m["hashes"])
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m["title"]))
            page = line_page
        else:
            if not buffer:
                page = line_page
            buffer.append(line)
    flush()
    return chunks


def _chunk_plain(source, title, lines, max_chars):
    """其他文件：以空行分段，再把相鄰段落裝填到字數上限。"""
    chunks, paragraphs, current, page = [], [], [], None
    for line_page, line in lines:
        if line.strip():
            if not current:
                page = line_page
            current.append(line)
        elif current:
            paragraphs.append((page, "\n".join(current)))
            current = []
    if current:
        paragraphs.append((page, "\n".join(current)))
    pack, pack_page = "", None
    for para_page, para in paragraphs:
        if pack and len(pack) + 2 + len(para) > max_chars:
            _emit(chunks, source, title, "", "", pack_page, pack, max_chars)
            pack = ""
        if not pack:
            pack_page = para_page
        pack = f"{pack}\n\n{para}" if pack else para
    if pack:
        _emit(chunks, source, title, "", "", pack_page, pack, max_chars)
    return chunks


def chunk_document(source: str, segments: List[Segment], max_chars: int = DEFAULT_MAX_CHARS) -> List[Chunk]:
    """依文件結構把內容切成 Chunk 清單；source 是檔名，title 取檔名（不含副檔名）。"""
    title = os.path.splitext(os.path.basename(source))[0]
    lines = _lines_with_page(segments)
    if sum(1 for _, line in lines if _ARTICLE_LINE.match(line)) >= _LEGAL_MIN_ARTICLES:
        return _chunk_legal(source, title, lines, max_chars)
    if sum(1 for _, line in lines if _MD_HEADING.match(line)) >= 2:
        return _chunk_markdown(source, title, lines, max_chars)
    return _chunk_plain(source, title, lines, max_chars)
