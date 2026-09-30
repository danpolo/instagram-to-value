#!/usr/bin/env python3
"""Scrape, track, and structure artifacts from https://glitchcatclub.com/lab.

Extracts all free artifacts shared by Kem (@kem_glitch), compares them against
the local manifest and disk state, stores new/updated artifacts structured
locally with metadata, Markdown documentation, and card images, and generates
RSS 2.0 and JSON feeds.

Usage:
    python3 scripts/scrape_lab_artifacts.py [--artifacts-dir DIR] [--dry-run]
        [--force] [--no-images] [--json] [--url URL]
"""
import argparse
import hashlib
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "https://glitchcatclub.com/lab"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts"
DEFAULT_TRANSCRIPTS_FILE = REPO_ROOT / "out" / "kem_glitch_transcripts.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def slugify(text: str) -> str:
    """Produce a deterministic, filesystem-safe kebab-case slug."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "item"


def compute_content_hash(title: str, quote: str, description: str, url: str) -> str:
    """Compute sha256 hash of core content fields to detect modifications."""
    raw = f"{title}\n{quote}\n{description}\n{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_artifact_uuid(url: str) -> Optional[str]:
    """Extract UUID or short identifier from Claude artifact URL."""
    # Examples:
    # https://claude.ai/code/artifact/bcbb16d8-f232-4b9d-b3f9-5ceeac85df83
    # https://claude.ai/artifact/VrYYBTppk5iGcGgkt7PWyK
    match = re.search(r"artifact/([0-9a-f-]{36}|[A-Za-z0-9_-]{10,})", url)
    return match.group(1) if match else None


def fetch_html(url: str = DEFAULT_URL, timeout: int = 15) -> str:
    """Fetch raw HTML from the target site with a browser-like User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def parse_artifacts_from_html(html: str, base_url: str = DEFAULT_URL) -> List[Dict[str, Any]]:
    """Parse artifact entries from the embedded JavaScript structures in the page."""
    # 1. Parse PIC dictionary: const PIC = { ... };
    pics: Dict[str, str] = {}
    pic_match = re.search(r"const\s+PIC\s*=\s*(\{.*?\});", html, re.DOTALL)
    if pic_match:
        try:
            pics = json.loads(pic_match.group(1))
        except Exception:
            pass

    # 2. Parse section titles and their grid IDs:
    # e.g.: <div class="sec"><span class="chip">01</span><h2>The artefacts</h2></div>
    #       <div class="grid" id="best"></div>
    sections_by_id: Dict[str, Tuple[str, str]] = {}  # id -> (chip, title)
    sec_pattern = re.compile(
        r'<div class="sec"><span class="chip">([^<]*)</span><h2>([^<]*)</h2></div>\s*<div class="grid" id="([^"]+)">',
        re.DOTALL,
    )
    for chip, title, grid_id in sec_pattern.findall(html):
        sections_by_id[grid_id.strip()] = (chip.strip(), title.strip())

    # 3. Parse fill() calls: fill('best', BEST, 1); fill('more', MORE, 9);
    fill_calls: Dict[str, Tuple[str, int]] = {}  # array_var_name -> (grid_id, start_index)
    fill_pattern = re.compile(r"fill\(\s*['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z0-9_]+)\s*,\s*(\d+)\s*\);")
    for grid_id, var_name, start_idx in fill_pattern.findall(html):
        fill_calls[var_name] = (grid_id, int(start_idx))

    # 4. Extract arrays: const BEST = [ ... ]; const MORE = [ ... ];
    # Search for all uppercase variable array definitions
    array_pattern = re.compile(r"const\s+([A-Z0-9_]+)\s*=\s*(\[[\s\S]*?\]);")
    artifacts: List[Dict[str, Any]] = []

    clean_base = base_url.rstrip("/")
    if clean_base.endswith("/lab"):
        img_base = f"{clean_base}/img"
    else:
        img_base = f"{clean_base}/lab/img"

    for var_name, array_raw in array_pattern.findall(html):
        if var_name == "PIC":
            continue
        try:
            items_list = json.loads(array_raw)
        except Exception:
            continue

        if not isinstance(items_list, list) or not items_list or not isinstance(items_list[0], list):
            continue

        grid_id, start_idx = fill_calls.get(var_name, ("unknown", len(artifacts) + 1))
        chip, section_title = sections_by_id.get(grid_id, ("", var_name.capitalize()))

        for offset, entry in enumerate(items_list):
            if not isinstance(entry, list) or len(entry) < 4:
                continue

            title = str(entry[0]).strip()
            quote = str(entry[1]).strip()
            description = str(entry[2]).strip()
            target_url = str(entry[3]).strip()
            color = str(entry[4]).strip() if len(entry) > 4 else "var(--gold)"
            dark_flag = bool(entry[5]) if len(entry) > 5 else False

            slug = slugify(title)
            uuid = extract_artifact_uuid(target_url)
            item_index = start_idx + offset

            pic_slug = pics.get(title)
            image_url = f"{img_base}/{pic_slug}.jpg" if pic_slug else None

            artifact = {
                "id": slug,
                "slug": slug,
                "title": title,
                "quote": quote,
                "description": description,
                "url": target_url,
                "artifact_uuid": uuid,
                "section": section_title,
                "section_id": grid_id,
                "section_chip": chip,
                "index": item_index,
                "color": color,
                "dark_quote": dark_flag,
                "image_name": pic_slug,
                "image_url": image_url,
                "content_hash": compute_content_hash(title, quote, description, target_url),
                "source_page": base_url,
            }
            artifacts.append(artifact)

    return artifacts


def load_kem_transcripts(transcripts_path: Path = DEFAULT_TRANSCRIPTS_FILE) -> Dict[str, Any]:
    """Load local Instagram video transcripts for @kem_glitch if available."""
    if not transcripts_path.is_file():
        return {}
    try:
        return json.loads(transcripts_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def correlate_with_reels(artifacts: List[Dict[str, Any]], transcripts: Dict[str, Any]) -> None:
    """Correlate artifacts with known transcribed Instagram reels from @kem_glitch."""
    if not transcripts:
        for a in artifacts:
            a["related_reels"] = []
        return

    # Keyword index for matching
    keywords_map = {
        "graph-engineering": ["knowledge graph", "graph engineering", "graph"],
        "cerebras-rag": ["cerebras", "cerberus", "rag", "embedding model"],
        "ste-tested": ["ste", "simplified technical english", "standard technical english"],
        "the-task-router": ["router", "model router", "cheapest model"],
        "orchestration": ["orchestration", "orchestrator", "brief"],
        "the-team-board": ["agent team", "team of four", "team board", "subagent"],
        "three-habits": ["habits", "vibe-coded", "tests first", "quality gates", "26 rules"],
        "loop-lego": ["loop", "agent loops", "four hooks"],
        "the-semantic-cache": ["cache", "semantic cache"],
    }

    for item in artifacts:
        slug = item["slug"]
        matches = []
        item_keywords = keywords_map.get(slug, [item["title"].lower()])

        for shortcode, data in transcripts.items():
            text_corpus = (
                f"{data.get('title', '')} {data.get('description', '')} {data.get('transcript', '')}"
            ).lower()

            hit = False
            for kw in item_keywords:
                if kw in text_corpus:
                    hit = True
                    break

            if hit:
                matches.append({
                    "shortcode": shortcode,
                    "title": data.get("title", f"Reel {shortcode}"),
                    "upload_date": data.get("upload_date"),
                    "instagram_url": f"https://www.instagram.com/p/{shortcode}/",
                })

        item["related_reels"] = matches


def load_manifest(manifest_path: Path) -> Dict[str, Any]:
    """Load existing artifacts manifest from disk."""
    if not manifest_path.is_file():
        return {"artifacts": {}, "last_scraped": None, "source_url": DEFAULT_URL}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {"artifacts": {}, "last_scraped": None, "source_url": DEFAULT_URL}


def save_manifest(manifest: Dict[str, Any], manifest_path: Path) -> None:
    """Atomically write manifest to disk."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp_path.replace(manifest_path)


def compare_artifacts(
    incoming: List[Dict[str, Any]],
    manifest: Dict[str, Any],
    items_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Compare incoming artifacts against manifest and disk state.

    Returns:
        (new_items, updated_items, unchanged_items)
    """
    known = manifest.get("artifacts", {})
    new_items: List[Dict[str, Any]] = []
    updated_items: List[Dict[str, Any]] = []
    unchanged_items: List[Dict[str, Any]] = []

    for item in incoming:
        slug = item["slug"]
        existing = known.get(slug)
        item_dir = items_dir / slug
        item_file = item_dir / "item.json"

        if existing is None and not item_file.exists():
            new_items.append(item)
        else:
            old_hash = existing.get("content_hash") if existing else None
            if not old_hash and item_file.exists():
                try:
                    on_disk = json.loads(item_file.read_text(encoding="utf-8"))
                    old_hash = on_disk.get("content_hash")
                except Exception:
                    old_hash = None

            if old_hash != item["content_hash"]:
                updated_items.append(item)
            else:
                unchanged_items.append(item)

    return new_items, updated_items, unchanged_items


def download_image(url: str, dest_path: Path, timeout: int = 15) -> bool:
    """Download card image if not already present or zero-sized."""
    if dest_path.is_file() and dest_path.stat().st_size > 0:
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if data:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(data)
                return True
    except Exception as err:
        sys.stderr.write(f"Warning: Failed to download {url}: {err}\n")
    return False


def write_artifact_item(
    item: Dict[str, Any],
    items_dir: Path,
    now_iso: str,
    download_images: bool = True,
) -> Path:
    """Write structured folder, item.json, README.md, and download image."""
    slug = item["slug"]
    item_dir = items_dir / slug
    item_dir.mkdir(parents=True, exist_ok=True)

    # First seen / updated timestamps
    item_json_path = item_dir / "item.json"
    first_seen = now_iso
    if item_json_path.exists():
        try:
            prior = json.loads(item_json_path.read_text(encoding="utf-8"))
            first_seen = prior.get("first_seen", now_iso)
        except Exception:
            pass

    record = dict(item)
    record["first_seen"] = first_seen
    record["last_updated"] = now_iso
    try:
        record["local_dir"] = str(item_dir.relative_to(REPO_ROOT))
    except ValueError:
        record["local_dir"] = str(item_dir)

    # Download image if available
    local_image_name = None
    if item.get("image_url") and download_images:
        img_dest = item_dir / "image.jpg"
        if download_image(item["image_url"], img_dest):
            local_image_name = "image.jpg"
            try:
                record["local_image"] = str(img_dest.relative_to(REPO_ROOT))
            except ValueError:
                record["local_image"] = str(img_dest)

    # Write item.json
    item_json_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Write README.md
    readme_lines = [
        f"# {item['title']}",
        "",
        f"> \"{item['quote']}\"",
        "",
        f"- **Category:** {item['section']} (#{item['index']:02d})",
        f"- **Direct Artifact Link:** [{item['url']}]({item['url']})",
        f"- **Source Page:** [{item['source_page']}]({item['source_page']})",
    ]

    if item.get("related_reels"):
        readme_lines.append("- **Related Instagram Reels:**")
        for reel in item["related_reels"]:
            date_str = f" ({reel['upload_date']})" if reel.get("upload_date") else ""
            readme_lines.append(f"  - [{reel['title']}]({reel['instagram_url']}){date_str}")

    readme_lines.extend([
        "",
        "## Description",
        "",
        item["description"],
        "",
    ])

    if local_image_name:
        readme_lines.extend([
            f"![{item['title']}]({local_image_name})",
            "",
        ])

    readme_path = item_dir / "README.md"
    readme_path.write_text("\n".join(readme_lines), encoding="utf-8")

    return item_dir


def generate_rss_feed(
    artifacts: List[Dict[str, Any]],
    output_path: Path,
    site_url: str = DEFAULT_URL,
) -> None:
    """Generate RSS 2.0 XML feed from artifacts list."""
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "The Lab · Glitch Cat Club Artefacts"
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = (
        "Every free artefact Kem has shared in his Instagram broadcast channel, in one place."
    )
    ET.SubElement(channel, "lastBuildDate").text = formatdate(usegmt=True)

    for item in artifacts:
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = f"{item['title']} · \"{item['quote']}\""
        ET.SubElement(entry, "link").text = item["url"]
        guid = ET.SubElement(entry, "guid", isPermaLink="false")
        guid.text = item.get("artifact_uuid") or item["url"]

        desc_html = (
            f"<p><strong>Problem:</strong> &ldquo;{item['quote']}&rdquo;</p>"
            f"<p>{item['description']}</p>"
            f"<p><a href=\"{item['url']}\">Open Artefact on Claude</a></p>"
        )
        ET.SubElement(entry, "description").text = desc_html
        ET.SubElement(entry, "category").text = item.get("section", "Artefact")

        if item.get("image_url"):
            ET.SubElement(
                entry,
                "enclosure",
                url=item["image_url"],
                type="image/jpeg",
                length="0",
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(str(output_path), encoding="utf-8", xml_declaration=True)


def generate_json_feed(
    artifacts: List[Dict[str, Any]],
    output_path: Path,
    site_url: str = DEFAULT_URL,
) -> None:
    """Generate JSON Feed v1.1 format."""
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "The Lab · Glitch Cat Club Artefacts",
        "home_page_url": site_url,
        "feed_url": f"{site_url.rstrip('/')}/feed.json",
        "description": "Every free artefact Kem has shared in his Instagram broadcast channel, in one place.",
        "items": [],
    }

    for item in artifacts:
        feed_item = {
            "id": item.get("artifact_uuid") or item["url"],
            "url": item["url"],
            "title": f"{item['title']} · \"{item['quote']}\"",
            "summary": item["description"],
            "content_text": f"Problem: \"{item['quote']}\"\n\n{item['description']}\n\nLink: {item['url']}",
            "tags": [item.get("section", "Artefact")],
        }
        if item.get("image_url"):
            feed_item["image"] = item["image_url"]
        feed["items"].append(feed_item)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def generate_index_markdown(
    artifacts: List[Dict[str, Any]],
    output_path: Path,
    site_url: str = DEFAULT_URL,
) -> None:
    """Generate INDEX.md catalog table for artifacts."""
    lines = [
        "# The Lab · Glitch Cat Club Artefacts",
        "",
        f"Synchronized copy of artifacts from [{site_url}]({site_url}).",
        "",
        "| # | Title | Problem / Hook | Category | Local Folder | Claude Link |",
        "|---|---|---|---|---|---|",
    ]

    for item in artifacts:
        slug = item["slug"]
        idx_str = f"{item['index']:02d}"
        local_link = f"[`{slug}`](items/{slug}/README.md)"
        claude_link = f"[Open]({item['url']})"
        clean_quote = item["quote"].replace("|", "\\|")
        lines.append(
            f"| {idx_str} | **{item['title']}** | {clean_quote} | {item['section']} | {local_link} | {claude_link} |"
        )

    lines.extend([
        "",
        "## Feeds",
        "",
        "- [RSS 2.0 Feed](feed.xml)",
        "- [JSON Feed v1.1](feed.json)",
        "- [Manifest](manifest.json)",
        "",
    ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def sync_lab_artifacts(
    url: str = DEFAULT_URL,
    artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR,
    transcripts_file: Path = DEFAULT_TRANSCRIPTS_FILE,
    dry_run: bool = False,
    force: bool = False,
    download_images: bool = True,
    raw_html: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute complete scrape, diff, structure, and feed generation workflow."""
    artifacts_dir = Path(artifacts_dir)
    items_dir = artifacts_dir / "items"
    manifest_path = artifacts_dir / "manifest.json"
    feed_xml_path = artifacts_dir / "feed.xml"
    feed_json_path = artifacts_dir / "feed.json"
    index_md_path = artifacts_dir / "INDEX.md"

    # 1. Fetch & Parse
    html = raw_html if raw_html is not None else fetch_html(url)
    incoming = parse_artifacts_from_html(html, base_url=url)
    if not incoming:
        return {
            "status": "warning",
            "message": "No artifacts found on page",
            "scraped_count": 0,
            "new_count": 0,
            "updated_count": 0,
            "unchanged_count": 0,
        }

    # 2. Correlate with Instagram reels
    transcripts = load_kem_transcripts(transcripts_file)
    correlate_with_reels(incoming, transcripts)

    # 3. Compare with existing state
    manifest = load_manifest(manifest_path)
    new_items, updated_items, unchanged_items = compare_artifacts(incoming, manifest, items_dir)

    to_write = (new_items + updated_items) if not force else incoming
    now_iso = datetime.now(timezone.utc).isoformat()

    if not dry_run:
        # Write individual items
        for item in to_write:
            write_artifact_item(item, items_dir, now_iso=now_iso, download_images=download_images)

        # Update manifest map
        artifacts_map = manifest.get("artifacts", {})
        for item in incoming:
            existing = artifacts_map.get(item["slug"], {})
            record = dict(item)
            record["first_seen"] = existing.get("first_seen", now_iso)
            record["last_updated"] = now_iso if item in to_write else existing.get("last_updated", now_iso)
            record["local_dir"] = f"items/{item['slug']}"
            if (items_dir / item["slug"] / "image.jpg").exists():
                record["local_image"] = f"items/{item['slug']}/image.jpg"
            artifacts_map[item["slug"]] = record

        updated_manifest = {
            "source_url": url,
            "last_scraped": now_iso,
            "total_artifacts": len(incoming),
            "artifacts": artifacts_map,
        }
        save_manifest(updated_manifest, manifest_path)

        # Generate feeds & index
        generate_rss_feed(incoming, feed_xml_path, site_url=url)
        generate_json_feed(incoming, feed_json_path, site_url=url)
        generate_index_markdown(incoming, index_md_path, site_url=url)

    return {
        "status": "ok",
        "dry_run": dry_run,
        "scraped_count": len(incoming),
        "new_count": len(new_items),
        "updated_count": len(updated_items),
        "unchanged_count": len(unchanged_items),
        "new_items": [{"title": i["title"], "slug": i["slug"], "url": i["url"]} for i in new_items],
        "updated_items": [{"title": i["title"], "slug": i["slug"], "url": i["url"]} for i in updated_items],
        "artifacts_dir": str(artifacts_dir),
        "manifest_path": str(manifest_path),
        "feed_xml_path": str(feed_xml_path),
        "feed_json_path": str(feed_json_path),
        "index_md_path": str(index_md_path),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Glitch Cat Club Lab artifacts, track new additions, and structure locally."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Source URL (default: {DEFAULT_URL})")
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=DEFAULT_ARTIFACTS_DIR,
        help=f"Directory to store structured artifacts (default: {DEFAULT_ARTIFACTS_DIR})",
    )
    parser.add_argument(
        "--transcripts-file",
        type=Path,
        default=DEFAULT_TRANSCRIPTS_FILE,
        help="Path to kem_glitch_transcripts.json",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report new/updated items without writing files")
    parser.add_argument("--force", action="store_true", help="Force rewriting of all artifacts even if unchanged")
    parser.add_argument("--no-images", action="store_true", help="Skip downloading card images")
    parser.add_argument("--json", action="store_true", help="Output summary in JSON format")

    args = parser.parse_args()

    result = sync_lab_artifacts(
        url=args.url,
        artifacts_dir=args.artifacts_dir,
        transcripts_file=args.transcripts_file,
        dry_run=args.dry_run,
        force=args.force,
        download_images=not args.no_images,
    )

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"{mode}Lab Artifact Scraper Finished")
    print(f"  Scraped from site: {result['scraped_count']}")
    print(f"  New artifacts:     {result['new_count']}")
    print(f"  Updated:           {result['updated_count']}")
    print(f"  Unchanged:         {result['unchanged_count']}")

    if result["new_items"]:
        print("\nNew additions:")
        for item in result["new_items"]:
            print(f"  + {item['title']} ({item['url']})")

    if result["updated_items"]:
        print("\nUpdated items:")
        for item in result["updated_items"]:
            print(f"  ~ {item['title']} ({item['url']})")

    if not args.dry_run:
        print(f"\nSaved structured artifacts to: {result['artifacts_dir']}")
        print(f"  Manifest: {result['manifest_path']}")
        print(f"  Index:    {result['index_md_path']}")
        print(f"  RSS Feed: {result['feed_xml_path']}")
        print(f"  JSON Feed:{result['feed_json_path']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
