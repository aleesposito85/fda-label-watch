"""Fetch FDA drug label snapshots from DailyMed and write per-section markdown.

For each drug listed in drugs.yaml:
  1. Hit /spls/{setid}/history.json to see the latest spl_version + date.
  2. If unchanged since last run (per data/{slug}/meta.yaml), skip.
  3. Otherwise fetch /spls/{setid}.xml, extract our target sections, and
     overwrite data/{slug}/*.md with one file per section.

Git is the database: each daily commit is a snapshot. Diffs come from `git log`.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DRUGS_FILE = ROOT / "drugs.yaml"

DAILYMED_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
SPL_NS = {"v3": "urn:hl7-org:v3"}


def _local(tag: str) -> str:
    """Strip the {namespace} prefix from an ElementTree tag."""
    return tag.split("}", 1)[1] if "}" in tag else tag

# LOINC codes for the high-signal sections we track.
# Order here defines display order.
SECTIONS: dict[str, str] = {
    "34066-1": "boxed_warning",
    "34067-9": "indications",
    "34068-7": "dosage_administration",
    "34070-3": "contraindications",
    "34071-1": "warnings",
    "43685-7": "warnings_precautions",
    "34084-4": "adverse_reactions",
    "34073-7": "drug_interactions",
    "43684-0": "use_specific_populations",
}

USER_AGENT = "fda-label-watch/0.1 (+https://github.com/your-org/fda-label-watch)"


@dataclass
class Drug:
    slug: str
    name: str
    setid: str
    notes: str = ""


def load_drugs() -> list[Drug]:
    cfg = yaml.safe_load(DRUGS_FILE.read_text())
    return [Drug(**d) for d in cfg["drugs"]]


def http_get(url: str, *, accept: str = "*/*") -> requests.Response:
    # DailyMed rejects Accept: application/xml or text/xml with 406; */* is fine.
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"unreachable: {url}")


def get_latest_version(setid: str) -> tuple[int, str] | None:
    """Return (spl_version, published_date) or None if unavailable."""
    url = f"{DAILYMED_BASE}/spls/{setid}/history.json"
    j = http_get(url).json()
    history = j.get("data", {}).get("history") or []
    if not history:
        return None
    latest = max(history, key=lambda h: h["spl_version"])
    return latest["spl_version"], latest["published_date"]


def fetch_spl_xml(setid: str) -> bytes:
    url = f"{DAILYMED_BASE}/spls/{setid}.xml"
    return http_get(url).content


def _text_of(elem: ET.Element) -> str:
    """Render an SPL <text> subtree into plain text with paragraph breaks.

    SPL uses <paragraph>, <list>/<item>, <table>, <content>, <br/>, etc.
    We flatten to text, marking paragraph and list-item boundaries with newlines.
    """
    out: list[str] = []
    tag = _local(elem.tag)

    if tag in ("paragraph", "title"):
        out.append(_inline_text(elem))
        out.append("\n\n")
    elif tag == "item":
        out.append("- " + _inline_text(elem))
        out.append("\n")
    elif tag == "list":
        for child in elem:
            out.append(_text_of(child))
        out.append("\n")
    elif tag == "table":
        # Simplified: just dump all cell text on lines.
        for row in elem.iter(f"{{{SPL_NS['v3']}}}tr"):
            cells = [
                _inline_text(c)
                for c in row
                if _local(c.tag) in ("td", "th")
            ]
            out.append(" | ".join(cells) + "\n")
        out.append("\n")
    else:
        # Recurse into anything else (text, content, caption, etc.)
        if elem.text and elem.text.strip():
            out.append(elem.text.strip() + " ")
        for child in elem:
            out.append(_text_of(child))
        if elem.tail and elem.tail.strip():
            out.append(elem.tail.strip() + " ")

    return "".join(out)


def _inline_text(elem: ET.Element) -> str:
    """Collect all text descendants as a single inline string (no paragraph breaks)."""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        local = _local(child.tag)
        if local == "br":
            parts.append(" ")
        else:
            parts.append(_inline_text(child))
        if child.tail:
            parts.append(child.tail)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


# Sentence boundary: period/!/? followed by whitespace and a capital letter or digit.
# Imperfect (Mr., e.g., 1.5 mg) but good enough for diff readability.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def normalize_for_diff(text: str) -> str:
    """One sentence per line; collapse runs of blank lines."""
    lines: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("- "):
            lines.extend(b.strip() for b in block.splitlines() if b.strip())
            lines.append("")
            continue
        for sent in _SENT_SPLIT.split(block):
            sent = sent.strip()
            if sent:
                lines.append(sent)
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def _render_section(section: ET.Element, depth: int, *, include_title: bool) -> str:
    """Render a <section> as text, including subsections via <component><section>.

    depth=0 corresponds to the top-level matched section. When include_title=False
    we skip rendering this section's own title (used for the outer call, since the
    caller emits a `# Title` header itself).
    """
    parts: list[str] = []
    if include_title:
        title_el = section.find("v3:title", SPL_NS)
        if title_el is not None:
            title = _inline_text(title_el)
            if title:
                heading = "#" * min(depth + 1, 6)
                parts.append(f"{heading} {title}\n\n")

    text_el = section.find("v3:text", SPL_NS)
    if text_el is not None:
        body = _text_of(text_el)
        if body.strip():
            parts.append(normalize_for_diff(body))
            parts.append("\n")

    for sub in section.findall("v3:component/v3:section", SPL_NS):
        parts.append(_render_section(sub, depth + 1, include_title=True))

    return "".join(parts)


def extract_sections(xml_bytes: bytes) -> dict[str, str]:
    """Return {section_slug: normalized_text} for sections matching SECTIONS.

    We only pick *outermost* matching sections — subsections are folded into
    their parent's output by _render_section so we don't double-extract them.
    """
    root = ET.fromstring(xml_bytes)
    found: dict[str, str] = {}
    seen_elements: set[int] = set()

    # First pass: collect all matching sections.
    matches: list[tuple[ET.Element, str]] = []
    for section in root.iter(f"{{{SPL_NS['v3']}}}section"):
        code_el = section.find("v3:code", SPL_NS)
        if code_el is None:
            continue
        code = code_el.attrib.get("code")
        if code in SECTIONS:
            matches.append((section, SECTIONS[code]))

    # Skip any section whose ancestor is also a match (avoid double-extraction).
    match_ids = {id(s) for s, _ in matches}

    def has_matching_ancestor(elem: ET.Element, parent_map: dict) -> bool:
        p = parent_map.get(id(elem))
        while p is not None:
            if id(p) in match_ids and id(p) != id(elem):
                return True
            p = parent_map.get(id(p))
        return False

    parent_map = {id(c): p for p in root.iter() for c in p}

    for section, slug in matches:
        if has_matching_ancestor(section, parent_map):
            continue
        title_el = section.find("v3:title", SPL_NS)
        code_el = section.find("v3:code", SPL_NS)
        title = (
            _inline_text(title_el) if title_el is not None
            else (code_el.attrib.get("displayName", "") if code_el is not None else slug)
        )
        body = _render_section(section, depth=0, include_title=False)
        rendered = f"# {title}\n\n{body}".rstrip() + "\n"
        if slug in found:
            found[slug] = found[slug].rstrip() + "\n\n" + rendered
        else:
            found[slug] = rendered

    return found


def write_drug(drug: Drug, sections: dict[str, str], spl_version: int, published_date: str, title: str) -> bool:
    """Write per-section markdown + meta.yaml. Return True if anything changed on disk."""
    drug_dir = DATA_DIR / drug.slug
    drug_dir.mkdir(parents=True, exist_ok=True)
    changed = False

    # Stable filenames in display order; missing sections produce empty files
    # so removed sections show as deletions in the diff.
    for code, slug in SECTIONS.items():
        path = drug_dir / f"{slug}.md"
        new_content = sections.get(slug, "").strip()
        if not new_content:
            new_content = f"# {slug.replace('_', ' ').title()}\n\n_(not present in this label)_\n"
        else:
            new_content = new_content.rstrip() + "\n"
        if not path.exists() or path.read_text() != new_content:
            path.write_text(new_content)
            changed = True

    meta_path = drug_dir / "meta.yaml"
    meta = {
        "slug": drug.slug,
        "name": drug.name,
        "setid": drug.setid,
        "title": title,
        "spl_version": spl_version,
        "published_date": published_date,
        "dailymed_url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={drug.setid}",
    }
    new_meta = yaml.safe_dump(meta, sort_keys=False)
    if not meta_path.exists() or meta_path.read_text() != new_meta:
        meta_path.write_text(new_meta)
        changed = True
    return changed


def read_existing_version(slug: str) -> tuple[int, str] | None:
    meta_path = DATA_DIR / slug / "meta.yaml"
    if not meta_path.exists():
        return None
    m = yaml.safe_load(meta_path.read_text()) or {}
    if "spl_version" in m and "published_date" in m:
        return m["spl_version"], m["published_date"]
    return None


def fetch_drug(drug: Drug, *, force: bool) -> str:
    """Run the fetch pipeline for one drug. Return a status string for logging."""
    latest = get_latest_version(drug.setid)
    if latest is None:
        return f"no history for {drug.slug}"
    spl_version, published_date = latest

    existing = read_existing_version(drug.slug)
    if not force and existing == (spl_version, published_date):
        return f"unchanged ({spl_version} / {published_date})"

    xml = fetch_spl_xml(drug.setid)
    # Title from the XML root <title>
    try:
        root = ET.fromstring(xml)
        title_el = root.find("v3:title", SPL_NS)
        title = _inline_text(title_el) if title_el is not None else drug.name
    except ET.ParseError:
        title = drug.name

    sections = extract_sections(xml)
    changed = write_drug(drug, sections, spl_version, published_date, title)
    suffix = "wrote" if changed else "no diff"
    return f"v{spl_version} {published_date}: {suffix} ({len(sections)} sections)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Re-fetch even if version unchanged")
    ap.add_argument("--only", help="Only process this slug (debug)")
    args = ap.parse_args()

    drugs = load_drugs()
    if args.only:
        drugs = [d for d in drugs if d.slug == args.only]
        if not drugs:
            print(f"no drug with slug={args.only}", file=sys.stderr)
            return 2

    fail = 0
    for drug in drugs:
        try:
            status = fetch_drug(drug, force=args.force)
            print(f"[{drug.slug}] {status}")
        except Exception as e:
            fail += 1
            print(f"[{drug.slug}] ERROR: {e}", file=sys.stderr)
        time.sleep(0.5)  # polite pacing for DailyMed
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
