"""File parsing + chunking.

`parse_file(file_type, path)` is the generic entry point / extension hook.

Two families of file_type are handled here:
  - "embedding" family (pdf, docx, image): parse_file() returns a
    list[Chunk] that the Celery task embeds and writes to Milvus.
  - "structured" family (xlsx, csv, json): these do NOT go through
    parse_file()/Chunk at all. Use `parse_structured(file_type, path)`
    (see bottom of this module) which returns pandas DataFrame(s) for the
    task to side-store as parquet (see tasks.py) - no embedding, no Milvus.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

# pypdf is the primary text-extraction path (pure python, no native deps).
import pypdf  # noqa: E402

# pdfplumber is used as a fallback for pages where pypdf extracts nothing
# (it handles some layouts pypdf struggles with) before we fall back to OCR.
try:
    import pdfplumber
except Exception:  # pragma: no cover - optional dep
    pdfplumber = None
    logger.warning("pdfplumber not available; pdfplumber fallback disabled")

# pytesseract/Pillow are optional at import time: the tesseract-ocr binary
# may not be installed in every environment (e.g. local dev without the
# apt package). We never want an import-time failure here to prevent the
# whole service from starting - OCR is a best-effort fallback for scanned
# pages, and a missing binary should just mean "skip OCR for that page".
try:
    import pytesseract
    from PIL import Image
except Exception:  # pragma: no cover - optional dep
    pytesseract = None
    Image = None
    logger.warning("pytesseract/Pillow not available; OCR fallback disabled")

# python-docx for DOCX parsing. Same "never block service startup" policy
# as the OCR deps above, even though python-docx is a required dependency
# in requirements.txt - defensive in case of a partial/broken install.
try:
    import docx as python_docx
except Exception:  # pragma: no cover - optional dep
    python_docx = None
    logger.warning("python-docx not available; DOCX parsing disabled")

# pandas (+ openpyxl for xlsx, pyarrow for parquet) for the structured
# (xlsx/csv/json) side-store path. Same defensive-import policy.
try:
    import pandas as pd
except Exception:  # pragma: no cover - optional dep
    pd = None
    logger.warning("pandas not available; xlsx/csv/json parsing disabled")


@dataclass
class Chunk:
    text: str
    source_ref: str  # e.g. "עמוד 12"
    chunk_index: int


def _ocr_page_to_text(page) -> str:
    """Best-effort OCR of a pypdf page via rendered image.

    pypdf itself cannot rasterize pages to images, so for OCR we rely on
    pdfplumber (which wraps pdfium/poppler-less rendering via pdfminer) to
    get a PIL image of the page. If that's unavailable, or tesseract isn't
    installed, we skip OCR for the page rather than crashing the task.
    """
    if pytesseract is None or Image is None:
        logger.warning("OCR requested but pytesseract/Pillow unavailable; skipping page")
        return ""
    return ""  # actual rasterization happens in _extract_pdf_pages via pdfplumber


def _extract_pdf_pages(path: str) -> list[str]:
    """Return a list of per-page text strings for a PDF.

    Strategy per page:
      1. Try pypdf text extraction.
      2. If empty, try pdfplumber text extraction (different extraction
         heuristics, sometimes succeeds where pypdf fails).
      3. If still empty (likely a scanned/image page), try OCR via
         pdfplumber's page.to_image() + pytesseract. If OCR isn't available
         or fails, log a warning and leave the page's text empty - we do
         NOT fail the whole document for one bad page.
    """
    pages_text: list[str] = []

    with open(path, "rb") as f:
        reader = pypdf.PdfReader(f)
        num_pages = len(reader.pages)
        pypdf_texts = []
        for i in range(num_pages):
            try:
                pypdf_texts.append(reader.pages[i].extract_text() or "")
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("pypdf failed to extract page %d: %s", i + 1, exc)
                pypdf_texts.append("")

    plumber_pages = None
    needs_plumber = any(not t.strip() for t in pypdf_texts)
    if needs_plumber and pdfplumber is not None:
        try:
            plumber_pages = pdfplumber.open(path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("pdfplumber failed to open %s: %s", path, exc)
            plumber_pages = None

    for i, text in enumerate(pypdf_texts):
        page_num = i + 1
        final_text = text or ""

        if not final_text.strip() and plumber_pages is not None:
            try:
                pb_page = plumber_pages.pages[i]
                plumber_text = pb_page.extract_text() or ""
                if plumber_text.strip():
                    final_text = plumber_text
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("pdfplumber failed on page %d: %s", page_num, exc)

        if not final_text.strip():
            # Still nothing -> likely a scanned page. Try OCR.
            if pytesseract is not None and plumber_pages is not None:
                try:
                    pb_page = plumber_pages.pages[i]
                    pil_image = pb_page.to_image(resolution=200).original
                    ocr_text = pytesseract.image_to_string(pil_image, lang="heb+eng")
                    if ocr_text.strip():
                        final_text = ocr_text
                        logger.info("OCR recovered text for page %d", page_num)
                    else:
                        logger.warning("OCR produced no text for page %d; skipping", page_num)
                except Exception as exc:
                    logger.warning(
                        "OCR failed for page %d (%s); skipping page, continuing task", page_num, exc
                    )
            else:
                logger.warning(
                    "Page %d has no extractable text and OCR is unavailable; skipping page",
                    page_num,
                )

        pages_text.append(final_text)

    if plumber_pages is not None:
        try:
            plumber_pages.close()
        except Exception:
            pass

    return pages_text


def _split_into_paragraphs(text: str) -> list[str]:
    # Split on blank-line boundaries first (paragraph breaks); fall back to
    # single newlines if the page has no blank lines at all.
    paras = re.split(r"\n\s*\n", text)
    if len(paras) <= 1:
        paras = text.split("\n")
    return [p.strip() for p in paras if p.strip()]


def _chunk_text(
    text: str,
    chunk_size_words: int = None,
    overlap_words: int = None,
) -> list[str]:
    """Chunk a single page's text into ~chunk_size_words-word windows with
    overlap, preferring to break on paragraph boundaries rather than mid-word
    when possible.
    """
    chunk_size_words = chunk_size_words or settings.CHUNK_SIZE_WORDS
    overlap_words = overlap_words or settings.CHUNK_OVERLAP_WORDS

    paragraphs = _split_into_paragraphs(text)
    if not paragraphs:
        return []

    # Build a flat list of words, remembering paragraph boundary positions,
    # so we can snap chunk boundaries to paragraph edges when close enough.
    words: list[str] = []
    boundary_word_indices: set[int] = set()
    for para in paragraphs:
        para_words = para.split()
        if not para_words:
            continue
        words.extend(para_words)
        boundary_word_indices.add(len(words))  # index right after this paragraph

    if not words:
        return []

    chunks: list[str] = []
    start = 0
    n = len(words)
    while start < n:
        end = min(start + chunk_size_words, n)

        if end < n:
            # Try to snap `end` to the nearest paragraph boundary within a
            # small lookahead/lookback window, to avoid splitting mid-paragraph.
            snap_window = max(10, chunk_size_words // 10)
            best = None
            for b in boundary_word_indices:
                if end - snap_window <= b <= end + snap_window and b > start:
                    if best is None or abs(b - end) < abs(best - end):
                        best = b
            if best is not None:
                end = best

        chunk_words = words[start:end]
        if chunk_words:
            chunks.append(" ".join(chunk_words))

        if end >= n:
            break
        start = max(end - overlap_words, start + 1)

    return chunks


def chunk_pages(pages_text: list[str]) -> list[Chunk]:
    """Chunk every page's text, tagging each chunk with its page-based
    source_ref ("עמוד N") per CONTRACTS.md citation format.
    """
    chunks: list[Chunk] = []
    idx = 0
    for page_num, text in enumerate(pages_text, start=1):
        if not text or not text.strip():
            continue
        for piece in _chunk_text(text):
            chunks.append(Chunk(text=piece, source_ref=f"עמוד {page_num}", chunk_index=idx))
            idx += 1
    return chunks


def parse_pdf(path: str) -> list[Chunk]:
    pages_text = _extract_pdf_pages(path)
    return chunk_pages(pages_text)


# --------------------------------------------------------------------------
# DOCX: heading-aware parsing.
# --------------------------------------------------------------------------

# Word's built-in heading styles are named "Heading 1", "Heading 2", ... We
# treat any style matching this as a section boundary; the heading level
# itself isn't used for anything beyond detecting "this paragraph starts a
# new section" (we don't build a nested outline, just flat sections).
_HEADING_STYLE_RE = re.compile(r"^Heading\s*\d+$", re.IGNORECASE)


def _is_heading_style(style_name: str | None) -> bool:
    return bool(style_name) and bool(_HEADING_STYLE_RE.match(style_name.strip()))


def _extract_docx_sections(path: str) -> list[tuple[str | None, str]]:
    """Walk a DOCX's paragraphs and group them into (heading, body_text)
    sections. `heading` is the nearest preceding Heading-style paragraph's
    text, or None if the document has no headings before this content (or
    no headings at all). Paragraphs are joined with blank lines so
    `_chunk_text`'s paragraph-boundary snapping still works within a
    section.
    """
    if python_docx is None:
        raise RuntimeError("python-docx is not available; cannot parse DOCX")

    document = python_docx.Document(path)

    sections: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    current_paras: list[str] = []

    def flush():
        if current_paras:
            body = "\n\n".join(current_paras)
            if body.strip():
                sections.append((current_heading, body))

    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if _is_heading_style(para.style.name if para.style else None):
            # New section starts here: flush whatever we had under the
            # previous heading, then start the new one. We intentionally do
            # NOT fold the heading text itself into the body - it becomes
            # the source_ref for the chunks that follow instead.
            flush()
            current_paras = []
            current_heading = text
        else:
            current_paras.append(text)

    flush()

    # No headings at all and no body paragraphs captured -> document had no
    # text (e.g. only images/tables). Caller handles the empty-list case.
    return sections


def parse_docx(path: str) -> list[Chunk]:
    """Heading-aware DOCX parsing: each section (delimited by Heading 1/2/3
    styles) is chunked independently so a chunk never straddles a heading
    boundary. source_ref is "סעיף: <heading text>" for sections under a
    heading, or "פסקה N" (1-indexed, per section-without-heading) when the
    document has no heading above that content.
    """
    sections = _extract_docx_sections(path)

    chunks: list[Chunk] = []
    idx = 0
    no_heading_counter = 0
    for heading, body in sections:
        if heading:
            source_ref = f"סעיף: {heading}"
        else:
            no_heading_counter += 1
            source_ref = f"פסקה {no_heading_counter}"

        for piece in _chunk_text(body):
            chunks.append(Chunk(text=piece, source_ref=source_ref, chunk_index=idx))
            idx += 1

    return chunks


# --------------------------------------------------------------------------
# Images: OCR-only (same pytesseract technique as the PDF scanned-page path).
# --------------------------------------------------------------------------


def parse_image(path: str) -> list[Chunk]:
    """OCR a standalone image file (jpg/png/etc). Mirrors the OCR fallback
    used for scanned PDF pages: best-effort, never raises on missing
    tesseract/empty result - just logs a warning and returns an empty chunk
    list so the caller marks the file 'ready' with zero chunks instead of
    'failed'.
    """
    if pytesseract is None or Image is None:
        logger.warning(
            "OCR requested for image %s but pytesseract/Pillow unavailable; "
            "returning zero chunks",
            path,
        )
        return []

    try:
        with Image.open(path) as img:
            text = pytesseract.image_to_string(img, lang="heb+eng")
    except Exception as exc:
        logger.warning("OCR failed for image %s (%s); returning zero chunks", path, exc)
        return []

    if not text or not text.strip():
        logger.warning("OCR produced no text for image %s; returning zero chunks", path)
        return []

    chunks: list[Chunk] = []
    for idx, piece in enumerate(_chunk_text(text)):
        chunks.append(Chunk(text=piece, source_ref="תמונה", chunk_index=idx))
    return chunks


def parse_file(file_type: str, path: str) -> list[Chunk]:
    """Extension hook: dispatch parsing by file_type, for the
    embedding/Milvus family of file types (pdf, docx, image).

    xlsx/csv/json are NOT handled here - they're a structured side-store
    (DataFrame -> parquet) path with no embedding/Milvus involved at all.
    See `parse_structured()` below, and app/tasks.py which routes between
    the two families based on file_type.
    """
    file_type = (file_type or "").lower().lstrip(".")
    if file_type == "pdf":
        return parse_pdf(path)
    if file_type == "docx":
        return parse_docx(path)
    if file_type in ("image", "jpg", "jpeg", "png", "bmp", "tiff", "gif", "webp"):
        return parse_image(path)
    raise NotImplementedError(
        f"parse_file: file_type='{file_type}' is not a supported embedding "
        "file type. pdf | docx | image are supported here; xlsx | csv | json "
        "go through parse_structured() instead (see CONTRACTS.md)."
    )


# --------------------------------------------------------------------------
# Structured side-store family: xlsx / csv / json -> pandas DataFrame(s).
#
# These never produce Chunk objects and never touch embeddings/Milvus - see
# CONTRACTS.md ("save-to-nfs -> parse-to-dataframe -> write-side-store (ל-
# xlsx/csv/json)"). app/tasks.py calls parse_structured() instead of
# parse_file() for these three file types, then writes the result(s) to
# parquet under NFS_ROOT_PATH/<conversation_id>/structured/ and records the
# resulting path in file_metadata.structured_store_ref.
# --------------------------------------------------------------------------


def parse_structured(file_type: str, path: str) -> dict[str, "pd.DataFrame"]:
    """Load xlsx/csv/json into one or more named pandas DataFrames.

    Returns a dict of {sheet_name: DataFrame}. For csv/json (always a single
    table) the dict has one entry under the key "data". For xlsx, every
    sheet in the workbook is loaded and keyed by its sheet name, so
    multi-sheet workbooks are preserved rather than collapsed into one
    table.
    """
    if pd is None:
        raise RuntimeError("pandas is not available; cannot parse xlsx/csv/json")

    file_type = (file_type or "").lower().lstrip(".")

    if file_type == "xlsx":
        # sheet_name=None -> dict of {sheet_name: DataFrame} for every sheet.
        sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
        return dict(sheets)

    if file_type == "csv":
        return {"data": pd.read_csv(path)}

    if file_type == "json":
        return {"data": pd.read_json(path)}

    raise NotImplementedError(
        f"parse_structured: file_type='{file_type}' is not a supported "
        "structured file type. Only xlsx | csv | json are handled here."
    )
