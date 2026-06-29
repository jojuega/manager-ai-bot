"""
domains.notion.sync — Notion → Flashcard Sync Engine.

Extracted from the original ``scripts/notion_sync.py`` (795 lines) monolith
into a domain module.  Responsibilities:

* talk to the Notion HTTP API (markdown fetch + page metadata);
* parse ``🎴`` / ``🧬`` / ``🃏`` flashcard markers out of a Notion page;
* sync those cards into the local ``course_flashcards`` table;
* maintain a small ``data/notion_sources.json`` config that lists the
  Notion root pages the bot subscribes to;
* build and cache a Notion page tree (``data/notion_tree.json``) used
  by the Flashcards menu.

Path / DB / env access goes through :mod:`core.config` and :mod:`core.db`
so this module no longer hardcodes ``/root/projects/jogtasksbot``.

Public surface
--------------
* :func:`run_sync` — entry point used by both the LLM tool and the
  Telegram ``/sync`` command.  Returns a structured result dict.
* :func:`sync_page` / :func:`sync_page_tree` — single-page helpers.
* :func:`parse_flashcards` — pure parser (handy for tests).
* :func:`rebuild_tree_cache` / :func:`get_tree` — menu cache.
* :func:`add_source` / :func:`remove_source` / :func:`load_sources` —
  source-list management.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import List, Tuple

from core.config import DATA, STATE_DB


# ─── Paths ────────────────────────────────────────────────────────────────────
CONFIG: Path = DATA / "notion_sources.json"
TREE_CACHE: Path = DATA / "notion_tree.json"


# ─── Notion API ───────────────────────────────────────────────────────────────
NOTION_VERSION = "2025-09-03"


def _load_env() -> None:
    """Load NOTION_API_KEY from ``$HERMES_HOME/.env`` if not already set.

    Mirrors the original ``notion_sync._load_env`` behaviour: read
    ``~/.hermes/.env`` once and ``setdefault`` so the real shell env
    always wins.
    """
    if os.environ.get("NOTION_API_KEY"):
        return
    env_path = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / ".env"
    if env_path.exists():
        for ln in env_path.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k, v)


def notion_api(method: str, path: str, data: dict = None) -> dict:
    """Call Notion API. Returns parsed JSON.

    Raises
    ------
    ValueError
        When ``NOTION_API_KEY`` is not set in the environment.
    RuntimeError
        When the upstream returns a non-2xx status (wraps the HTTP
        error body for easier debugging).
    """
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        raise ValueError("NOTION_API_KEY not set in .env")
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    else:
        body = None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_text = e.read().decode()
        raise RuntimeError(f"Notion API {method} {path}: {e.code} {err_text}")


def notion_page_markdown(page_id: str) -> Tuple[str, str]:
    """Get page content as markdown + page title.

    Returns ``(markdown, title)``.  When the ``/markdown`` endpoint
    isn't available (older Notion API versions), ``markdown`` is empty
    but the title is still resolved.
    """
    # Get page metadata for title
    meta = notion_api("GET", f"pages/{page_id}")
    title = ""
    if "properties" in meta:
        for prop in meta["properties"].values():
            if prop.get("type") == "title":
                parts = prop.get("title", [])
                if parts:
                    title = "".join(t.get("plain_text", "") for t in parts)
                break

    # Get markdown content
    try:
        resp = notion_api("GET", f"pages/{page_id}/markdown")
        markdown = resp.get("markdown", "")
    except RuntimeError:
        markdown = ""

    return markdown, title


# ─── Parser ────────────────────────────────────────────────────────────────────

def parse_flashcards(markdown: str, page_id: str, page_title: str) -> list:
    """Extract 🎴/🧬/🃏 flashcards from Notion markdown.

    Card types:
      🎴 normal:   ``🎴 front`` and indented ``= back``
      🧬 double:   ``🧬 front`` and indented ``= back`` (creates 2 cards: normal + reversed)
      🃏 reversed: ``🃏 back`` and indented ``= front`` (what the user must recall)

    Returns a list of dicts with keys:
        card_type, front, back, source, course, source_id
    """
    cards = []
    lines = markdown.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent_level = len(line) - len(line.lstrip())

        # Detect emoji markers at any indent level
        marker = None
        front_text = ""
        if stripped.startswith("🎴"):
            marker = "notion"  # normal
            front_text = stripped[1:].strip()
        elif stripped.startswith("🧬"):
            marker = "notion_double"
            front_text = stripped[1:].strip()
        elif stripped.startswith("🃏"):
            marker = "notion_reversed"
            front_text = stripped[1:].strip()

        if marker is None:
            i += 1
            continue

        # Collect indented lines that follow (more indented than the marker)
        back_parts = []
        i += 1
        while i < len(lines):
            next_line = lines[i]
            next_stripped = next_line.strip()
            if not next_stripped:
                # Empty line = separator, skip
                i += 1
                continue
            next_indent = len(next_line) - len(next_line.lstrip())
            if next_indent > indent_level:
                # Direct child: first level of indentation
                back_parts.append(next_stripped)
                i += 1
                # Continue collecting at same indent level or deeper
                while i < len(lines):
                    nl = lines[i]
                    ns = nl.strip()
                    if not ns:
                        i += 1
                        continue
                    ni = len(nl) - len(nl.lstrip())
                    if ni <= indent_level:
                        break  # back to marker level = new card
                    back_parts.append(ns)
                    i += 1
            else:
                break  # not indented = not part of this card

        back_text = "\n".join(back_parts)

        # Skip empty cards
        if not front_text and not back_text:
            continue

        # Create stable source_id from page + front hash
        raw = f"{page_id}:{front_text}:{back_text}"
        content_hash = hashlib.md5(raw.encode()).hexdigest()[:16]
        notion_block_id = f"ntn_{page_id[:8]}_{content_hash}"

        source_url = f"https://notion.so/{page_id.replace('-', '')}"

        if marker == "notion":
            cards.append({
                "card_type": "notion",
                "front": front_text,
                "back": back_text,
                "source": source_url,
                "course": f"📖 {page_title}" if page_title else "Notion",
                "source_id": notion_block_id,
            })
        elif marker == "notion_double":
            # Normal card
            cards.append({
                "card_type": "notion",
                "front": front_text,
                "back": back_text,
                "source": source_url,
                "course": f"📖 {page_title}" if page_title else "Notion",
                "source_id": notion_block_id,
            })
            # Reversed card
            cards.append({
                "card_type": "notion_reversed",
                "front": back_text,
                "back": front_text,
                "source": source_url,
                "course": f"📖 {page_title}" if page_title else "Notion",
                "source_id": notion_block_id + "_rev",
            })
        elif marker == "notion_reversed":
            cards.append({
                "card_type": "notion_reversed",
                "front": back_text,
                "back": front_text,
                "source": source_url,
                "course": f"📖 {page_title}" if page_title else "Notion",
                "source_id": notion_block_id,
            })

    return cards


# ─── DB ────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Get connection to ``state.db``, ensuring schema is up to date.

    The original ``notion_sync`` had its own ``get_db`` plus an
    inline ``PRAGMA table_info`` migration to add the ``source_id``
    column.  The migration now lives in :func:`core.db._migrate_state_source_id`
    and is run once at startup; this helper just opens the connection.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_child_page_ids(page_id: str, max_depth: int = 3, _depth: int = 0) -> list:
    """Recursively get all child page IDs under a page.

    Scans blocks for ``child_page`` blocks.  Respects ``max_depth`` to
    prevent infinite loops.  Returns a list of ``(page_id, title, depth)``.
    """
    if _depth >= max_depth:
        return []
    results = []
    try:
        data = notion_api("GET", f"blocks/{page_id}/children")
        for block in data.get("results", []):
            if block.get("type") == "child_page":
                cid = block["id"]
                ctitle = block.get("child_page", {}).get("title", "")
                results.append((cid, ctitle, _depth + 1))
                # Recurse into this child
                results.extend(get_child_page_ids(cid, max_depth, _depth + 1))
    except Exception:
        pass  # skip pages we can't access
    return results


def sync_page_tree(root_page_id: str, dry_run: bool = False) -> dict:
    """Sync flashcards from a page AND all its child pages (recursive, depth=3).

    Returns aggregated stats dict with a ``page_results`` list.
    """
    # Gather all pages: root + children
    all_pages = [(root_page_id, "", 0)]
    all_pages.extend(get_child_page_ids(root_page_id))

    # Deduplicate by page_id
    seen = set()
    unique_pages = []
    for pid, title, depth in all_pages:
        if pid not in seen:
            seen.add(pid)
            unique_pages.append((pid, title, depth))

    # Sync each page
    page_results = []
    total = {"cards_found": 0, "added": 0, "updated": 0, "removed": 0}

    for pid, title, depth in unique_pages:
        try:
            r = sync_page(pid, dry_run)
            r["depth"] = depth
            r["title"] = title or r.get("title", "")
            page_results.append(r)
            total["cards_found"] += r.get("cards_found", 0)
            total["added"] += r.get("added", 0)
            total["updated"] += r.get("updated", 0)
            total["removed"] += r.get("removed", 0)
        except Exception as e:
            page_results.append({"page": pid, "title": title, "depth": depth, "status": "error", "error": str(e)})

    # Rebuild tree cache after sync (best-effort)
    try:
        rebuild_tree_cache()
    except Exception:
        pass

    return {
        "status": "ok",
        "pages_scanned": len(unique_pages),
        "page_results": page_results,
        **total,
    }


def sync_page(page_id: str, dry_run: bool = False) -> dict:
    """Sync flashcards from one Notion page. Returns stats dict."""
    markdown, title = notion_page_markdown(page_id)
    if not markdown:
        return {"page": page_id, "title": title, "status": "no_content", "added": 0, "updated": 0, "removed": 0}

    extracted = parse_flashcards(markdown, page_id, title)

    if dry_run:
        return {
            "page": page_id,
            "title": title,
            "status": "dry_run",
            "cards_found": len(extracted),
            "cards": extracted,
        }

    # Write to DB
    conn = get_db()
    today = date.today().isoformat()
    added = 0
    updated = 0

    for card in extracted:
        existing = conn.execute(
            "SELECT id, front, back FROM course_flashcards WHERE source_id=?",
            (card["source_id"],),
        ).fetchone()

        if existing is None:
            # Insert new card
            conn.execute(
                """INSERT INTO course_flashcards
                   (course, card_type, front, back, source, source_id, ease, interval, repetitions, next_review, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 2.5, 0, 0, ?, ?)""",
                (card["course"], card["card_type"], card["front"], card["back"],
                 card["source"], card["source_id"], today, datetime.now().isoformat()),
            )
            added += 1
        elif existing["front"] != card["front"] or existing["back"] != card["back"]:
            # Content changed → update + reset SRS
            conn.execute(
                """UPDATE course_flashcards SET front=?, back=?, course=?, ease=2.5, interval=0,
                   repetitions=0, next_review=? WHERE id=?""",
                (card["front"], card["back"], card["course"], today, existing["id"]),
            )
            updated += 1

    # Remove cards whose source_ids are no longer in the page
    if extracted:
        source_ids = tuple(c["source_id"] for c in extracted)
        placeholders = ",".join("?" for _ in source_ids)
        # Only remove cards from this page (by source URL prefix)
        page_url_prefix = f"https://notion.so/{page_id.replace('-', '')}"
        deleted = conn.execute(
            f"""DELETE FROM course_flashcards WHERE source LIKE ?
                AND source_id NOT IN ({placeholders})""",
            (f"{page_url_prefix}%",) + source_ids,
        ).rowcount
    else:
        page_url_prefix = f"https://notion.so/{page_id.replace('-', '')}"
        deleted = conn.execute(
            "DELETE FROM course_flashcards WHERE source LIKE ?",
            (f"{page_url_prefix}%",),
        ).rowcount

    conn.commit()
    conn.close()

    return {
        "page": page_id,
        "title": title,
        "status": "ok",
        "cards_found": len(extracted),
        "added": added,
        "updated": updated,
        "removed": deleted,
    }


# ─── Tree Cache ────────────────────────────────────────────────────────────────

def build_page_tree(page_id: str, max_depth: int = 3, _depth: int = 0) -> dict:
    """Build a tree of Notion pages with card counts from DB.

    Returns a dict with keys ``page_id``, ``title``, ``has_conceptual``,
    ``factual_count``, ``conceptual_count``, ``children``.
    """
    title = ""
    has_conceptual = False
    children = []

    # Get page metadata
    try:
        meta = notion_api("GET", f"pages/{page_id}")
        for prop in meta.get("properties", {}).values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                break
        has_conceptual = title.strip().endswith("🎴")
    except Exception:
        title = page_id[:8]

    # Get children
    if _depth < max_depth:
        try:
            data = notion_api("GET", f"blocks/{page_id}/children")
            for block in data.get("results", []):
                if block.get("type") == "child_page":
                    cnode = build_page_tree(block["id"], max_depth, _depth + 1)
                    children.append(cnode)
        except Exception:
            pass

    # Count cards from DB
    url_prefix = f"https://notion.so/{page_id.replace('-', '')}%"
    factual_count = 0
    conceptual_count = 0
    try:
        conn = get_db()
        for row in conn.execute(
            "SELECT card_type, COUNT(*) as cnt FROM course_flashcards WHERE source LIKE ? GROUP BY card_type",
            (url_prefix,),
        ).fetchall():
            ct = row["card_type"]
            if ct == "notion":
                factual_count += row["cnt"]
            elif ct == "notion_reversed":
                factual_count += row["cnt"]  # reversed is still factual
            elif ct == "notion_conceptual":
                conceptual_count += row["cnt"]
        conn.close()
    except Exception:
        pass

    # Include child counts
    for child in children:
        factual_count += child.get("factual_count", 0)
        conceptual_count += child.get("conceptual_count", 0)

    return {
        "page_id": page_id,
        "title": title,
        "has_conceptual": has_conceptual,
        "factual_count": factual_count,
        "conceptual_count": conceptual_count,
        "children": children,
    }


def rebuild_tree_cache() -> dict:
    """Rebuild the tree cache from all configured sources."""
    sources = load_sources()
    tree = {"sources": {}}
    for src in sources.get("sources", []):
        try:
            root = build_page_tree(src["page_id"])
            tree["sources"][src["page_id"]] = root
        except Exception as e:
            tree["sources"][src["page_id"]] = {"page_id": src["page_id"], "title": src.get("title", ""), "error": str(e)}
    DATA.mkdir(parents=True, exist_ok=True)
    TREE_CACHE.write_text(json.dumps(tree, indent=2, ensure_ascii=False))
    return tree


def get_tree() -> dict:
    """Get cached tree, rebuilding if missing."""
    if TREE_CACHE.exists():
        return json.loads(TREE_CACHE.read_text())
    return rebuild_tree_cache()


# ─── Source Management ────────────────────────────────────────────────────────

def load_sources() -> dict:
    """Load configured Notion source pages."""
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {"sources": []}


def save_sources(sources: dict) -> None:
    """Persist the ``notion_sources.json`` config to disk."""
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(sources, indent=2, ensure_ascii=False))


def add_source(page_id: str, title: str = "") -> str:
    """Add a Notion root page to the subscribed sources list.

    Returns a human-readable status string suitable for surfacing in a
    Telegram reply.
    """
    sources = load_sources()
    # Check if exists
    for s in sources["sources"]:
        if s["page_id"] == page_id:
            if title:
                s["title"] = title
            save_sources(sources)
            return f"✅ Source already exists: {s.get('title', page_id)}"
    sources["sources"].append({"page_id": page_id, "title": title or page_id})
    save_sources(sources)
    return f"✅ Added source: {title or page_id}"


def remove_source(page_id: str) -> str:
    """Remove a Notion root page from the subscribed sources list.

    Returns a human-readable status string suitable for surfacing in a
    Telegram reply.
    """
    sources = load_sources()
    before = len(sources["sources"])
    sources["sources"] = [s for s in sources["sources"] if s["page_id"] != page_id]
    if len(sources["sources"]) < before:
        save_sources(sources)
        return f"✅ Removed source: {page_id}"
    return f"❌ Source not found: {page_id}"


# ─── Public API ───────────────────────────────────────────────────────────────

def run_sync(timeout_sec: int = 120, page_id: str = None, verbose: bool = True) -> dict:
    """Run the Notion sync. Pure-Python entry point.

    Can be called in-process from another script (e.g. the bot's
    ``/sync`` command, the LLM tool) instead of spawning a subprocess.

    Args:
        timeout_sec: Wall-clock deadline in seconds for the whole run.
            If exceeded while iterating sources, the loop stops, remaining
            sources are skipped, and the partial result is still returned
            with the error recorded. Default 120.
        page_id: If given, sync only this single page (and its child pages
            recursively up to depth 3). If None, sync every configured
            source from ``data/notion_sources.json``.
        verbose: If True, print per-source/per-page progress lines (CLI
            default). If False, run silently for in-process callers that
            only care about the return value.

    Returns:
        Dict with keys:
          status         ``"ok" | "error"``
          page_id        echo of input (None for all-sources mode)
          sources        list of per-source result dicts (from sync_page_tree)
          pages_scanned  total number of pages visited
          cards_found    total flashcards parsed across all pages
          added          number of new cards created
          updated        number of existing cards updated
          removed        number of stale cards deleted
          cards_created  alias of ``added`` (convenience for callers)
          errors         list of human-readable error messages
    """
    _load_env()
    started = time.monotonic()

    def _budget_left() -> bool:
        return (time.monotonic() - started) < timeout_sec

    def _log(msg: str) -> None:
        if verbose:
            print(msg)

    sources_results = []
    errors = []
    total_pages = 0
    total_found = 0
    total_added = 0
    total_updated = 0
    total_removed = 0

    # Single-page mode
    if page_id:
        try:
            r = sync_page_tree(page_id)
            sources_results.append(r)
            total_pages += r.get("pages_scanned", 0)
            total_found += r.get("cards_found", 0)
            total_added += r.get("added", 0)
            total_updated += r.get("updated", 0)
            total_removed += r.get("removed", 0)
        except Exception as e:
            errors.append(f"{page_id}: {e}")
            _log(f"[ERROR] {page_id}: {e}")
        return {
            "status": "ok" if not errors else "error",
            "page_id": page_id,
            "sources": sources_results,
            "pages_scanned": total_pages,
            "cards_found": total_found,
            "added": total_added,
            "updated": total_updated,
            "removed": total_removed,
            "cards_created": total_added,
            "errors": errors,
        }

    # All-sources mode
    sources = load_sources()
    if not sources.get("sources"):
        msg = "No sources configured. Use 'add-source' first."
        _log(json.dumps({"error": msg}))
        return {
            "status": "error",
            "page_id": None,
            "sources": [],
            "pages_scanned": 0,
            "cards_found": 0,
            "added": 0,
            "updated": 0,
            "removed": 0,
            "cards_created": 0,
            "errors": [msg],
        }

    for src in sources["sources"]:
        if not _budget_left():
            errors.append(f"timeout: aborted before source {src['page_id']}")
            _log(f"[TIMEOUT] {src.get('title', src['page_id'])}")
            break
        try:
            r = sync_page_tree(src["page_id"])
            sources_results.append(r)
            total_pages += r.get("pages_scanned", 0)
            total_found += r.get("cards_found", 0)
            total_added += r.get("added", 0)
            total_updated += r.get("updated", 0)
            total_removed += r.get("removed", 0)
        except Exception as e:
            errors.append(f"{src['page_id']}: {e}")
            _log(f"[ERROR] {src.get('title', src['page_id'])}: {e}")

    return {
        "status": "ok" if not errors else "error",
        "page_id": None,
        "sources": sources_results,
        "pages_scanned": total_pages,
        "cards_found": total_found,
        "added": total_added,
        "updated": total_updated,
        "removed": total_removed,
        "cards_created": total_added,
        "errors": errors,
    }


__all__ = [
    "CONFIG",
    "TREE_CACHE",
    "NOTION_VERSION",
    "notion_api",
    "notion_page_markdown",
    "parse_flashcards",
    "get_db",
    "get_child_page_ids",
    "sync_page",
    "sync_page_tree",
    "build_page_tree",
    "rebuild_tree_cache",
    "get_tree",
    "load_sources",
    "save_sources",
    "add_source",
    "remove_source",
    "run_sync",
]
