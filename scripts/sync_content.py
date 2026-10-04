"""Turn the website's Markdown content into the assistant's knowledge base.

The site and the assistant share ONE source of truth: web/src/content/. This
script converts every content entry into a plain-text document in api/rag/docs/
and writes api/rag/docs/sources.json, which maps each document to the title and
URL of the page it came from. The retrieval engine stores that mapping with each
chunk, so a citation can link straight to the right page.

    web/src/content/insights/notice-to-quit-lagos.md
        -> api/rag/docs/insights--notice-to-quit-lagos.txt
        -> sources.json: {"insights--notice-to-quit-lagos.txt":
                          {"title": "...", "url": "/insights/notice-to-quit-lagos/", ...}}

For a client build: edit the content, run this script, then rebuild the index
(python api/rag/engine.py build --rebuild). Nothing else changes.

    python scripts/sync_content.py
"""

import json
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTENT = ROOT / "web" / "src" / "content"
DOCS = ROOT / "api" / "rag" / "docs"
MANIFEST = DOCS / "sources.json"

# Collection folder -> page URL. Must match the routes in web/src/pages/.
URLS = {
    "pages": lambda id_: f"/{id_}/",
    "practice": lambda id_: f"/practice/{id_}/",
    "insights": lambda id_: f"/insights/{id_}/",
    "people": lambda id_: f"/people/#{id_}",
}


def split_frontmatter(raw):
    """Return (frontmatter dict, Markdown body)."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", raw, re.DOTALL)
    if not m:
        return {}, raw
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def to_plain(md):
    """Markdown -> paragraph-structured plain text for the chunker.

    The engine splits on blank lines, so every heading and list item becomes its
    own paragraph. That keeps one fact per paragraph instead of a whole list
    collapsing into a single run-on line.
    """
    out = []
    for line in md.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line:
            out.append("")
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)                 # headings
        line = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", line)        # list markers
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)  # links/images -> text
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)            # bold
        line = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", line)  # italics
        line = line.replace("`", "")
        out.append(line)
        out.append("")                                         # one paragraph per line
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def practice_titles():
    titles = {}
    for path in (CONTENT / "practice").glob("*.md"):
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        titles[path.stem] = fm["title"]
    return titles


def build_doc(collection, id_, fm, body, practices):
    """Return (title, document text) for one content entry."""
    if collection == "people":
        title = f"{fm['name']}, {fm['role']}"
        areas = ", ".join(practices.get(p, p) for p in fm.get("practices", []))
        header = [title, f"Practice areas: {areas}."]
    elif collection == "insights":
        title = fm["title"]
        header = [
            title,
            fm["summary"],
            f"Insight article by {fm['author']}, published {fm['date']:%d %B %Y}. "
            f"Practice area: {practices.get(fm['practice'], fm['practice'])}. "
            f"Sources: {'; '.join(fm.get('sources', []))}.",
        ]
    else:
        title = fm["title"]
        header = [title, fm["summary"]]
    return title, "\n\n".join(header) + "\n\n" + to_plain(body)


def main():
    DOCS.mkdir(parents=True, exist_ok=True)
    # Clear only what this script generates, so stale pages never linger.
    for old in DOCS.glob("*--*.txt"):
        old.unlink()

    practices = practice_titles()
    manifest = {}
    for collection, url_for in URLS.items():
        for path in sorted((CONTENT / collection).glob("*.md")):
            id_ = path.stem
            fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
            title, text = build_doc(collection, id_, fm, body, practices)
            name = f"{collection}--{id_}.txt"
            (DOCS / name).write_text(text + "\n", encoding="utf-8")
            manifest[name] = {"title": title, "url": url_for(id_), "kind": collection, "id": id_}

    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    counts = {}
    for entry in manifest.values():
        counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
    print(f"Wrote {len(manifest)} documents to {DOCS.relative_to(ROOT)}: "
          + ", ".join(f"{n} {k}" for k, n in counts.items()))


if __name__ == "__main__":
    main()
