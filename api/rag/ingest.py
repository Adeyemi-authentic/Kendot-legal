"""Unified document ingestion: turn a mixed docs/ folder into clean plain text.

The retrieval engine embeds and stores TEXT; where that text comes from is this
module's job. It is the one place that knows how to read a source file, so the
engine, the pgvector engine, and the evaluator all load the corpus the same way.

Supported inputs (add a format here, everything downstream just works):
    .txt / .md   read directly (UTF-8)
    .pdf         extracted page-by-page with pypdf

Cardinal RAG rule: garbage in, garbage out. A PDF's raw text extraction is
line-wrapped, hyphenated across line breaks, and littered with page furniture.
`normalize()` repairs that into paragraph-structured prose BEFORE it reaches the
chunker (which splits on blank lines), so a fact is never severed mid-word and a
whole document never collapses into a single un-splittable chunk.

CLI:
    python ingest.py            # list what the corpus loads to (name -> chars, paras)
    python ingest.py <file>     # print the cleaned text of one source file
"""

import pathlib
import re
import sys

SUPPORTED = {".txt", ".md", ".pdf"}

# Recurring navigation / page furniture seen in converted legal PDFs. A line
# that is ONLY one of these (case-insensitive) is dropped. Keep this generic;
# corpus-specific cleaning belongs in a one-off prep step, not the shared loader.
_NOISE_LINE = re.compile(
    r"^\s*(back to top|nigerian[\s-]*constitution(\.com)?|page \d+)\s*$",
    re.IGNORECASE,
)
# A line that is nothing but a page number (e.g. "23").
_PAGE_NUM = re.compile(r"^\s*\d{1,4}\s*$")
# Markdown image refs / embedded data-URIs from a converted PDF.
_IMAGE_LINE = re.compile(r"^\s*(!?\[image\d*\]|\[image\d*\]:|<?data:image/)", re.IGNORECASE)


def _read_pdf(path: pathlib.Path) -> str:
    """Extract text from a PDF, page by page, joined by blank lines.

    pypdf is a pure-Python reader (no system deps), so this runs anywhere the
    rest of the stack does. Each page becomes its own paragraph block; damaged
    or image-only pages simply contribute nothing.
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - clear message beats a stack trace
        raise SystemExit(
            "pypdf is required to ingest PDFs. Install it: pip install pypdf"
        ) from e

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text)
    return "\n\n".join(pages)


def normalize(text: str) -> str:
    """Repair extracted text into clean, paragraph-structured prose.

    Steps, in order:
      1. de-hyphenate words split across a line break ("provi-\\nsion" -> "provision")
      2. drop page furniture (nav boilerplate, bare page numbers, image refs)
      3. strip markdown emphasis/escapes carried over from a .md conversion
      4. rejoin lines within a paragraph, preserving blank-line paragraph breaks
         (the chunker splits on blank lines, so those boundaries must survive)
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    # 1. join hyphenated line-break splits.
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    # 2 + 3. clean line by line.
    cleaned = []
    for line in text.split("\n"):
        if _NOISE_LINE.match(line) or _PAGE_NUM.match(line) or _IMAGE_LINE.match(line):
            cleaned.append("")            # becomes a paragraph break, not deleted text
            continue
        line = line.replace("\\.", ".").replace("\\-", "-")  # md backslash-escapes
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)          # bold
        line = re.sub(r"\*(.*?)\*", r"\1", line)              # italic
        line = line.replace("*", "")
        cleaned.append(line.rstrip())
    text = "\n".join(cleaned)

    # 4. collapse single newlines inside a paragraph to spaces; keep blank-line
    #    breaks. Protect paragraph boundaries with a sentinel (a char that never
    #    occurs in text) before flattening intra-paragraph line wraps.
    sent = "\x00"
    text = re.sub(r"\n[ \t]*\n+", sent, text)       # 1+ blank lines -> paragraph break
    text = text.replace("\n", " ")                  # intra-paragraph wraps -> spaces
    text = text.replace(sent, "\n\n")               # restore paragraph breaks
    text = re.sub(r"[ \t]{2,}", " ", text)          # squeeze runs of spaces
    text = re.sub(r"\n{3,}", "\n\n", text)          # never more than one blank line
    return text.strip()


def load_documents(docs_dir: pathlib.Path):
    """Yield (filename, clean_text) for every supported file in docs_dir, sorted.

    Empty results (e.g. an image-only PDF) are skipped so they never become an
    empty chunk. This is the single corpus entry point for the whole system.
    """
    for path in sorted(docs_dir.iterdir()):
        if path.suffix.lower() not in SUPPORTED:
            continue
        raw = _read_pdf(path) if path.suffix.lower() == ".pdf" else path.read_text(encoding="utf-8")
        text = normalize(raw)
        if text:
            yield path.name, text


def main():
    HERE = pathlib.Path(__file__).resolve().parent
    args = sys.argv[1:]
    if args:
        p = pathlib.Path(args[0])
        raw = _read_pdf(p) if p.suffix.lower() == ".pdf" else p.read_text(encoding="utf-8")
        print(normalize(raw))
        return
    docs_dir = HERE / "docs"
    total = 0
    for name, text in load_documents(docs_dir):
        paras = [p for p in text.split("\n\n") if p.strip()]
        print(f"  {name:<44} {len(text):>7} chars   {len(paras):>4} paragraphs")
        total += 1
    print(f"\n{total} document(s) loaded from {docs_dir}")


if __name__ == "__main__":
    main()
