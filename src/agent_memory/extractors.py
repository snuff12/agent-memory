from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree

from .chunking import ChunkDraft, chunk_text, discover_title
from .config import AppConfig
from .python_chunking import chunk_python
from .util import normalize_text


TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".bib",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs", ".c", ".h", ".cc", ".cpp",
    ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".zsh", ".fish",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".csv", ".tsv", ".sql", ".graphql", ".css", ".scss", ".xml",
}
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".hwpx"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | OFFICE_EXTENSIONS | {".pdf", ".html", ".htm"}


@dataclass(frozen=True)
class ExtractedUnit:
    text: str
    locator: str = ""
    page: Optional[int] = None
    line_offset: int = 1
    markdown_headings: bool = False


@dataclass(frozen=True)
class ExtractionResult:
    title: str
    extractor: str
    chunks: tuple[ChunkDraft, ...]
    warning: str = ""


class _VisibleHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        elif tag.casefold() in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        elif tag.casefold() in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data)


def _read_text(path: Path, limit: int) -> str:
    if path.stat().st_size > limit:
        raise ValueError("text file exceeds configured byte limit")
    payload = path.read_bytes()
    if b"\x00" in payload[:8192]:
        raise ValueError("binary content detected")
    return normalize_text(payload.decode("utf-8-sig", errors="replace"))


def _extract_html(path: Path, config: AppConfig) -> List[ExtractedUnit]:
    parser = _VisibleHTMLParser()
    parser.feed(_read_text(path, config.max_text_file_bytes))
    visible = html.unescape("".join(parser.parts))
    visible = re.sub(r"[ \t]+", " ", visible)
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    return [ExtractedUnit(text=visible, markdown_headings=False)] if visible else []


def _safe_zip(path: Path, config: AppConfig) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path)
    total = sum(info.file_size for info in archive.infolist())
    if total > config.max_archive_uncompressed_bytes:
        archive.close()
        raise ValueError("archive exceeds configured uncompressed byte limit")
    if any(info.file_size > config.max_archive_uncompressed_bytes for info in archive.infolist()):
        archive.close()
        raise ValueError("archive member exceeds configured byte limit")
    return archive


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(payload: bytes, paragraph_tags: set[str]) -> str:
    root = ElementTree.fromstring(payload)
    parts: List[str] = []
    for element in root.iter():
        name = _localname(element.tag)
        if name in {"t", "text"} and element.text:
            parts.append(element.text)
        elif name in paragraph_tags and parts and parts[-1] != "\n":
            parts.append("\n")
    joined = " ".join(part for part in parts if part != "\n") if "\n" not in parts else "".join(parts)
    return re.sub(r"\n{3,}", "\n\n", normalize_text(joined)).strip()


def _numeric_name_key(name: str):  # type: ignore[no-untyped-def]
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def _xlsx_text(archive: zipfile.ZipFile, sheet_name: str, shared_strings: List[str]) -> str:
    root = ElementTree.fromstring(archive.read(sheet_name))
    rows: List[str] = []
    for row in (element for element in root.iter() if _localname(element.tag) == "row"):
        cells: List[str] = []
        for cell in (element for element in row if _localname(element.tag) == "c"):
            cell_type = cell.attrib.get("t", "")
            values = [
                child.text or ""
                for child in cell.iter()
                if _localname(child.tag) in ({"t"} if cell_type == "inlineStr" else {"v"})
            ]
            raw = "".join(values).strip()
            if cell_type == "s" and raw:
                try:
                    value = shared_strings[int(raw)]
                except (ValueError, IndexError):
                    value = ""
            else:
                value = raw
            if value:
                cells.append(value)
        if cells:
            rows.append("\t".join(cells))
    return normalize_text("\n".join(rows)).strip()


def _extract_office(path: Path, config: AppConfig) -> List[ExtractedUnit]:
    suffix = path.suffix.casefold()
    with _safe_zip(path, config) as archive:
        names = archive.namelist()
        units: List[ExtractedUnit] = []
        if suffix == ".docx":
            candidates = [name for name in names if name == "word/document.xml"]
            for name in candidates:
                text = _xml_text(archive.read(name), {"p", "tr"})
                if text:
                    units.append(ExtractedUnit(text=text, locator="document"))
        elif suffix == ".pptx":
            candidates = sorted(
                (name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=_numeric_name_key,
            )
            for index, name in enumerate(candidates, 1):
                text = _xml_text(archive.read(name), {"p"})
                if text:
                    units.append(ExtractedUnit(text=text, locator="slide %d" % index))
        elif suffix == ".hwpx":
            candidates = sorted(
                (name for name in names if re.fullmatch(r"Contents/section\d+\.xml", name, re.IGNORECASE)),
                key=_numeric_name_key,
            )
            for index, name in enumerate(candidates, 1):
                text = _xml_text(archive.read(name), {"p", "tr"})
                if text:
                    units.append(ExtractedUnit(text=text, locator="section %d" % index))
        elif suffix == ".xlsx":
            shared_strings: List[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in (element for element in shared_root.iter() if _localname(element.tag) == "si"):
                    shared_strings.append(
                        "".join(
                            child.text or ""
                            for child in item.iter()
                            if _localname(child.tag) == "t"
                        )
                    )
            candidates = sorted(
                (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
                key=_numeric_name_key,
            )
            for index, name in enumerate(candidates, 1):
                text = _xlsx_text(archive, name, shared_strings)
                if text:
                    units.append(ExtractedUnit(text=text, locator="sheet %d" % index))
        return units


def _extract_pdf(path: Path, config: AppConfig) -> tuple[List[ExtractedUnit], str]:
    if path.stat().st_size > config.max_pdf_file_bytes:
        raise ValueError("PDF exceeds configured byte limit")
    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]

        document = pdfium.PdfDocument(str(path))
        try:
            if len(document) > 5000:
                raise ValueError("PDF page count exceeds safety limit")
            units = []
            for index in range(len(document)):
                page = document[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = normalize_text(text_page.get_text_range() or "").strip()
                    finally:
                        text_page.close()
                finally:
                    page.close()
                if text:
                    units.append(ExtractedUnit(text=text, locator="page %d" % (index + 1), page=index + 1))
            return units, "pdfium"
        finally:
            document.close()
    except ImportError:
        pass

    # Pure-Python fallback for platforms where a PDFium wheel is unavailable.
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("PDF parser unavailable; install the project dependencies") from exc

    reader = PdfReader(str(path), strict=False)
    if len(reader.pages) > 5000:
        raise ValueError("PDF page count exceeds safety limit")
    units = []
    for index, page in enumerate(reader.pages, 1):
        text = normalize_text(page.extract_text() or "").strip()
        if text:
            units.append(ExtractedUnit(text=text, locator="page %d" % index, page=index))
    return units, "pypdf"


def extract_file(path: Path, config: AppConfig) -> ExtractionResult:
    suffix = path.suffix.casefold()
    if suffix in TEXT_EXTENSIONS:
        text = _read_text(path, config.max_text_file_bytes)
        units = [
            ExtractedUnit(
                text=text,
                markdown_headings=suffix in {".md", ".markdown", ".org", ".rst"},
            )
        ]
        extractor = "text"
    elif suffix in {".html", ".htm"}:
        units = _extract_html(path, config)
        extractor = "html"
    elif suffix in OFFICE_EXTENSIONS:
        units = _extract_office(path, config)
        extractor = suffix.lstrip(".")
    elif suffix == ".pdf":
        units, extractor = _extract_pdf(path, config)
    else:
        raise ValueError("unsupported extension: %s" % suffix)

    if suffix == ".py" and config.python_chunking == "ast":
        return ExtractionResult(path.stem, "python-ast", tuple(chunk_python(text, config.chunk_chars, config.chunk_overlap_chars)))

    drafts: List[ChunkDraft] = []
    for unit in units:
        unit_chunks = chunk_text(
            unit.text,
            target_chars=config.chunk_chars,
            overlap_chars=config.chunk_overlap_chars,
            locator=unit.locator,
            page=unit.page,
            line_offset=unit.line_offset,
            markdown_headings=unit.markdown_headings,
        )
        drafts.extend(unit_chunks)

    drafts = [
        ChunkDraft(
            ordinal=index,
            heading=draft.heading,
            locator=draft.locator,
            start_line=draft.start_line,
            end_line=draft.end_line,
            page=draft.page,
            text=draft.text,
            token_estimate=draft.token_estimate,
        )
        for index, draft in enumerate(drafts)
    ]
    sample = units[0].text if units else ""
    title = discover_title(sample, path.stem.replace("-", " ").replace("_", " "))
    warning = ""
    if not drafts:
        warning = "no extractable text"
    return ExtractionResult(title=title, extractor=extractor, chunks=tuple(drafts), warning=warning)
