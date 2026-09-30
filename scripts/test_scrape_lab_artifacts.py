import json
import xml.etree.ElementTree as ET
from pathlib import Path
import pytest

from scrape_lab_artifacts import (
    slugify,
    compute_content_hash,
    extract_artifact_uuid,
    parse_artifacts_from_html,
    correlate_with_reels,
    compare_artifacts,
    write_artifact_item,
    generate_rss_feed,
    generate_json_feed,
    generate_index_markdown,
    sync_lab_artifacts,
)

SAMPLE_HTML = """<!doctype html>
<html>
<head><title>The Lab</title></head>
<body>
  <div class="sec"><span class="chip">01</span><h2>The artefacts</h2></div>
  <div class="grid" id="best"></div>
  <div class="sec"><span class="chip">02</span><h2>Bonus</h2></div>
  <div class="grid" id="more"></div>

<script>
const BEST=[
 ["Graph Engineering","My agent says done. It isn't.","When a job needs a chat, a loop or a graph.","https://claude.ai/code/artifact/bcbb16d8-f232-4b9d-b3f9-5ceeac85df83","var(--pink)",1],
 ["Cerebras RAG","It never finds my old notes.","Your notes, searchable by meaning.","https://claude.ai/code/artifact/2b1ebd30-a4e7-4022-8773-b27e1b5dc285","var(--yellow)"]
];
const MORE=[
 ["The Trifecta","Where do I even start?","The three foundations of prompting.","https://claude.ai/code/artifact/51e3aebb-af8d-4aac-970f-dd809adca7ca","var(--mint)"]
];
const PIC={"Graph Engineering":"graph-engineering","Cerebras RAG":"cerebras-rag"};
fill('best',BEST,1);
fill('more',MORE,3);
</script>
</body>
</html>
"""


def test_slugify():
    assert slugify("Graph Engineering") == "graph-engineering"
    assert slugify("STE, Tested") == "ste-tested"
    assert slugify("   Special #$% Characters   ") == "special-characters"
    assert slugify("") == "item"


def test_extract_artifact_uuid():
    u1 = "https://claude.ai/code/artifact/bcbb16d8-f232-4b9d-b3f9-5ceeac85df83"
    assert extract_artifact_uuid(u1) == "bcbb16d8-f232-4b9d-b3f9-5ceeac85df83"

    u2 = "https://claude.ai/artifact/VrYYBTppk5iGcGgkt7PWyK"
    assert extract_artifact_uuid(u2) == "VrYYBTppk5iGcGgkt7PWyK"

    assert extract_artifact_uuid("https://example.com/nothing") is None


def test_content_hash():
    h1 = compute_content_hash("Title", "Quote", "Desc", "https://url.com")
    h2 = compute_content_hash("Title", "Quote", "Desc", "https://url.com")
    h3 = compute_content_hash("Title 2", "Quote", "Desc", "https://url.com")
    assert h1 == h2
    assert h1 != h3


def test_parse_artifacts_from_html():
    artifacts = parse_artifacts_from_html(SAMPLE_HTML, base_url="https://glitchcatclub.com/lab")
    assert len(artifacts) == 3

    item0 = artifacts[0]
    assert item0["title"] == "Graph Engineering"
    assert item0["slug"] == "graph-engineering"
    assert item0["quote"] == "My agent says done. It isn't."
    assert item0["section"] == "The artefacts"
    assert item0["section_id"] == "best"
    assert item0["index"] == 1
    assert item0["image_name"] == "graph-engineering"
    assert item0["image_url"] == "https://glitchcatclub.com/lab/img/graph-engineering.jpg"
    assert item0["dark_quote"] is True

    item1 = artifacts[1]
    assert item1["title"] == "Cerebras RAG"
    assert item1["index"] == 2

    item2 = artifacts[2]
    assert item2["title"] == "The Trifecta"
    assert item2["section"] == "Bonus"
    assert item2["section_id"] == "more"
    assert item2["index"] == 3
    assert item2["image_name"] is None
    assert item2["image_url"] is None


def test_correlate_with_reels():
    artifacts = parse_artifacts_from_html(SAMPLE_HTML)
    transcripts = {
        "DcqtyTStpsw": {
            "title": "Video by kem_glitch",
            "description": "Free Cerberus RAG system and some bonus!",
            "transcript": "You've all heard of RAG? It's a semantic layer...",
            "upload_date": "20260830",
        }
    }
    correlate_with_reels(artifacts, transcripts)

    # Cerebras RAG should match
    rag_item = next(a for a in artifacts if a["slug"] == "cerebras-rag")
    assert len(rag_item["related_reels"]) == 1
    assert rag_item["related_reels"][0]["shortcode"] == "DcqtyTStpsw"

    # The Trifecta has no matching transcript in this mock
    trifecta_item = next(a for a in artifacts if a["slug"] == "the-trifecta")
    assert len(trifecta_item["related_reels"]) == 0


def test_compare_artifacts(tmp_path):
    items_dir = tmp_path / "items"
    items_dir.mkdir()

    artifacts = parse_artifacts_from_html(SAMPLE_HTML)
    manifest = {"artifacts": {}}

    # Initially all are new
    new_items, updated_items, unchanged_items = compare_artifacts(artifacts, manifest, items_dir)
    assert len(new_items) == 3
    assert len(updated_items) == 0
    assert len(unchanged_items) == 0

    # Simulate one existing unchanged, one updated
    manifest["artifacts"] = {
        "graph-engineering": {
            "content_hash": artifacts[0]["content_hash"],
        },
        "cerebras-rag": {
            "content_hash": "old_outdated_hash",
        },
    }

    new_items2, updated_items2, unchanged_items2 = compare_artifacts(artifacts, manifest, items_dir)
    assert len(unchanged_items2) == 1
    assert unchanged_items2[0]["slug"] == "graph-engineering"
    assert len(updated_items2) == 1
    assert updated_items2[0]["slug"] == "cerebras-rag"
    assert len(new_items2) == 1
    assert new_items2[0]["slug"] == "the-trifecta"


def test_write_artifact_item(tmp_path):
    items_dir = tmp_path / "items"
    artifacts = parse_artifacts_from_html(SAMPLE_HTML)
    item = artifacts[0]

    out_dir = write_artifact_item(item, items_dir, now_iso="2026-09-28T12:00:00Z", download_images=False)
    assert out_dir.exists()
    assert (out_dir / "item.json").exists()
    assert (out_dir / "README.md").exists()

    data = json.loads((out_dir / "item.json").read_text())
    assert data["title"] == "Graph Engineering"
    assert data["first_seen"] == "2026-09-28T12:00:00Z"

    readme = (out_dir / "README.md").read_text()
    assert "# Graph Engineering" in readme
    assert '“My agent says done. It isn\'t.”' in readme or '"My agent says done. It isn\'t."' in readme


def test_feeds_and_index_generation(tmp_path):
    artifacts = parse_artifacts_from_html(SAMPLE_HTML)
    feed_xml = tmp_path / "feed.xml"
    feed_json = tmp_path / "feed.json"
    index_md = tmp_path / "INDEX.md"

    generate_rss_feed(artifacts, feed_xml)
    assert feed_xml.exists()
    # Verify XML is valid
    tree = ET.parse(str(feed_xml))
    root = tree.getroot()
    assert root.tag == "rss"
    items = root.findall("./channel/item")
    assert len(items) == 3

    generate_json_feed(artifacts, feed_json)
    assert feed_json.exists()
    feed_data = json.loads(feed_json.read_text())
    assert feed_data["version"] == "https://jsonfeed.org/version/1.1"
    assert len(feed_data["items"]) == 3

    generate_index_markdown(artifacts, index_md)
    assert index_md.exists()
    index_text = index_md.read_text()
    assert "Graph Engineering" in index_text
    assert "Cerebras RAG" in index_text


def test_sync_lab_artifacts_end_to_end(tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    transcripts_file = tmp_path / "transcripts.json"
    transcripts_file.write_text(json.dumps({}))

    # 1. First run with SAMPLE_HTML (adds 3 items)
    res1 = sync_lab_artifacts(
        artifacts_dir=artifacts_dir,
        transcripts_file=transcripts_file,
        raw_html=SAMPLE_HTML,
        download_images=False,
    )
    assert res1["status"] == "ok"
    assert res1["scraped_count"] == 3
    assert res1["new_count"] == 3
    assert res1["updated_count"] == 0
    assert res1["unchanged_count"] == 0

    assert (artifacts_dir / "manifest.json").exists()
    assert (artifacts_dir / "feed.xml").exists()
    assert (artifacts_dir / "feed.json").exists()
    assert (artifacts_dir / "INDEX.md").exists()
    assert (artifacts_dir / "items" / "graph-engineering" / "item.json").exists()

    # 2. Second run without changes (idempotent, 0 new)
    res2 = sync_lab_artifacts(
        artifacts_dir=artifacts_dir,
        transcripts_file=transcripts_file,
        raw_html=SAMPLE_HTML,
        download_images=False,
    )
    assert res2["new_count"] == 0
    assert res2["unchanged_count"] == 3

    # 3. Dry run with modified HTML (1 updated)
    modified_html = SAMPLE_HTML.replace("When a job needs a chat, a loop or a graph.", "Updated description.")
    res3 = sync_lab_artifacts(
        artifacts_dir=artifacts_dir,
        transcripts_file=transcripts_file,
        raw_html=modified_html,
        dry_run=True,
        download_images=False,
    )
    assert res3["dry_run"] is True
    assert res3["updated_count"] == 1
    assert res3["unchanged_count"] == 2
    # Ensure dry run did not overwrite item.json
    on_disk = json.loads((artifacts_dir / "items" / "graph-engineering" / "item.json").read_text())
    assert on_disk["description"] == "When a job needs a chat, a loop or a graph."
