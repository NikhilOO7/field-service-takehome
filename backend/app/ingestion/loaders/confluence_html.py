"""Confluence HTML export loader (wiki pages).

- Title from <title> ('Field Service : X'), space path from the breadcrumbs, author and last-modified
  date from the page metadata line.
- Body split by h1-h4; info/warning/note panels become warning blocks; tables become rows.
- Comments are indexed as their own blocks with author and date (they carry facts and corrections).
- Pages in a former user's personal space are marked unofficial.
"""

import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from app.ingestion.types import Block, ParsedDocument, Section

POSTED_RE = re.compile(r"Posted by (.+?) at (\w{3} \d{1,2}, \d{4} \d{1,2}:\d{2})")
META_RE = re.compile(r"Created by (.+?), last modified (?:by .+? )?on (\w{3} \d{1,2}, \d{4})")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _parse_date(value: str, fmt: str) -> str | None:
    try:
        return datetime.strptime(value, fmt).date().isoformat()
    except ValueError:
        return None


def load_confluence(path: Path) -> ParsedDocument:
    out = ParsedDocument()
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")

    title = _text(soup.title) if soup.title else path.stem
    title = title.split(" : ", 1)[-1]
    breadcrumbs = [_text(li) for li in soup.select("#breadcrumbs li")]
    meta_div = soup.find(class_="page-metadata")
    meta_text = _text(meta_div) if meta_div else ""
    m = META_RE.search(meta_text)
    out.metadata = {
        "title": title,
        "space_path": breadcrumbs,
        "author": m.group(1).strip() if m else None,
        "last_modified": _parse_date(m.group(2), "%b %d, %Y") if m else None,
        "personal_space": any("Former user" in b for b in breadcrumbs) or "Former user" in title,
    }

    content = soup.find(id="main-content") or soup.find(class_="wiki-content") or soup.body
    for junk in content.select(".toc-macro, script, style"):
        junk.decompose()

    sections: list[Section] = [Section(heading_path=["(page)"])]
    stack: list[tuple[int, str]] = []

    def add(kind: str, text: str, merge: str | None, extra: dict | None = None) -> None:
        if not text:
            return
        cur = sections[-1]
        loc = {"heading": cur.heading_path[-1]}
        if extra:
            loc.update(extra)
        cur.blocks.append(Block(kind=kind, text=text, locator=loc, merge_key=merge))

    def walk(node: Tag) -> None:
        nonlocal stack
        for child in node.children:
            if isinstance(child, NavigableString):
                if child.strip():
                    add("prose", re.sub(r"\s+", " ", child.strip()), f"sec:{len(sections)}")
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            classes = child.get("class", [])
            if name in {"h1", "h2", "h3", "h4"}:
                depth = int(name[1])
                heading = _text(child)
                stack = [s for s in stack if s[0] < depth] + [(depth, heading)]
                sections.append(Section(heading_path=[t for _, t in stack]))
            elif "confluence-information-macro" in classes:
                kind = "warning" if any(c.endswith(("-warning", "-note")) for c in classes) else "prose"
                add(kind, _text(child), None, {"panel": True})
            elif name == "table" or "table-wrap" in classes:
                table = child if name == "table" else child.find("table")
                if table is None:
                    continue
                rows = [[_text(c) for c in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
                rows = [r for r in rows if any(r)]
                if not rows:
                    continue
                header = rows[0] if table.find("th") else None
                body = rows[1:] if header else rows
                key = f"tbl:{len(sections)}:{id(table)}"
                for r_i, row in enumerate(body, start=1):
                    text = " | ".join(f"{h}: {v}" for h, v in zip(header, row) if v) if header else " | ".join(row)
                    add("table_row", text, key, {"row": r_i, "label": row[0] if row else None})
            elif name in {"ul", "ol"}:
                for li in child.find_all("li", recursive=False):
                    add("prose", "• " + _text(li), f"sec:{len(sections)}")
            elif name in {"p", "pre", "blockquote"}:
                add("prose", _text(child), f"sec:{len(sections)}")
            elif "expand-container" in classes:
                walk(child.find(class_="expand-content") or child)
            elif name in {"div", "span", "section"}:
                walk(child)
            else:
                text = _text(child)
                if text:
                    add("prose", text, f"sec:{len(sections)}")

    walk(content)

    # Comments (outside main-content in the export)
    comment_section = Section(heading_path=["Comments"])
    for td in soup.select(".pageSection td"):
        body = td.find(class_="smallfont")
        posted = td.find("div", class_="smallfont")
        if body is None or posted is None:
            continue
        pm = POSTED_RE.search(_text(posted))
        author = pm.group(1) if pm else None
        when = _parse_date(pm.group(2), "%b %d, %Y %H:%M") if pm else None
        anchor = td.find("a", attrs={"name": True})
        comment_section.blocks.append(
            Block(
                kind="comment",
                text=f"Comment by {author} ({when}): {_text(body)}",
                locator={"comment_id": anchor["name"] if anchor else None, "author": author, "date": when},
                event_date=datetime.fromisoformat(when).date() if when else None,
            )
        )
    sections.append(comment_section)

    out.sections = [s for s in sections if s.blocks]
    if not out.sections:
        out.parse_status = "failed"
        out.parse_notes.append("no content extracted")
    return out
