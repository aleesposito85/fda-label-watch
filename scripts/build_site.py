"""Build a static HTML site from the markdown snapshots in data/.

Reads:
  - drugs.yaml              (which drugs to render)
  - data/{slug}/meta.yaml   (current metadata)
  - data/{slug}/*.md        (current section content)
  - git log -- data/{slug}/ (version history → diffs)

Writes:
  - site/index.html              list of tracked drugs
  - site/changes.html            chronological feed across all drugs
  - site/feed.xml                Atom feed of changes
  - site/drugs/{slug}/index.html per-drug page with timeline + inline diffs
  - site/static/styles.css

The site is fully self-contained — no JS, no external resources.
"""
from __future__ import annotations

import argparse
import difflib
import html
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DRUGS_FILE = ROOT / "drugs.yaml"
SITE_DIR = ROOT / "site"
TEMPLATES_DIR = ROOT / "scripts" / "templates"

SECTION_ORDER = [
    "boxed_warning",
    "indications",
    "dosage_administration",
    "contraindications",
    "warnings",
    "warnings_precautions",
    "adverse_reactions",
    "drug_interactions",
    "use_specific_populations",
]
SECTION_LABELS = {
    "boxed_warning": "Boxed Warning",
    "indications": "Indications & Usage",
    "dosage_administration": "Dosage & Administration",
    "contraindications": "Contraindications",
    "warnings": "Warnings",
    "warnings_precautions": "Warnings & Precautions",
    "adverse_reactions": "Adverse Reactions",
    "drug_interactions": "Drug Interactions",
    "use_specific_populations": "Use in Specific Populations",
}


@dataclass
class Commit:
    sha: str
    date: datetime
    subject: str


@dataclass
class Version:
    """A snapshot of a drug at a single commit."""
    commit: Commit
    meta: dict
    sections: dict[str, str]  # slug -> markdown content


@dataclass
class Change:
    """A diff between two consecutive versions of a drug for one section."""
    drug_slug: str
    drug_name: str
    section_slug: str
    section_label: str
    from_version: int | None
    to_version: int | None
    when: datetime
    diff_html: str


def git(*args: str, cwd: Path = ROOT) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def have_git_history() -> bool:
    """Return True if the repo has at least one commit."""
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return r.returncode == 0


def commits_for_path(rel_path: str) -> list[Commit]:
    """Commits that touched rel_path, oldest first."""
    if not have_git_history():
        return []
    out = git("log", "--reverse", "--pretty=format:%H|%aI|%s", "--", rel_path)
    commits: list[Commit] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, iso, subject = line.split("|", 2)
        commits.append(Commit(sha, datetime.fromisoformat(iso), subject))
    return commits


def file_at_commit(sha: str, rel_path: str) -> str | None:
    r = subprocess.run(
        ["git", "show", f"{sha}:{rel_path}"], cwd=ROOT, capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    return r.stdout


def load_version_from_disk(slug: str) -> Version | None:
    drug_dir = DATA_DIR / slug
    meta_path = drug_dir / "meta.yaml"
    if not meta_path.exists():
        return None
    meta = yaml.safe_load(meta_path.read_text()) or {}
    sections: dict[str, str] = {}
    for s in SECTION_ORDER:
        p = drug_dir / f"{s}.md"
        sections[s] = p.read_text() if p.exists() else ""
    fake_commit = Commit(sha="WORKING", date=datetime.now(timezone.utc), subject="(working tree)")
    return Version(commit=fake_commit, meta=meta, sections=sections)


def load_version_from_commit(slug: str, commit: Commit) -> Version | None:
    rel_dir = f"data/{slug}"
    meta_text = file_at_commit(commit.sha, f"{rel_dir}/meta.yaml")
    if meta_text is None:
        return None
    meta = yaml.safe_load(meta_text) or {}
    sections: dict[str, str] = {}
    for s in SECTION_ORDER:
        content = file_at_commit(commit.sha, f"{rel_dir}/{s}.md")
        sections[s] = content or ""
    return Version(commit=commit, meta=meta, sections=sections)


# ---- markdown -> HTML (tiny, hand-rolled) ----------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(?:[-*])\s+(.*)$")


def md_to_html(md: str) -> str:
    """Convert our constrained markdown subset to HTML.

    Supports: # h1 .. ###### h6, blank-line paragraphs, "- " bullets, plain text.
    Italic markers like _x_ are passed through (escaped).
    """
    lines = md.splitlines()
    out: list[str] = []
    in_list = False
    para: list[str] = []

    def flush_para():
        nonlocal para
        if para:
            text = " ".join(para).strip()
            if text:
                out.append(f"<p>{html.escape(text)}</p>")
            para = []

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        s = line.rstrip()
        if not s:
            flush_para()
            close_list()
            continue
        m = _HEADING_RE.match(s)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{html.escape(m.group(2))}</h{level}>")
            continue
        b = _BULLET_RE.match(s)
        if b:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html.escape(b.group(1))}</li>")
            continue
        para.append(s)
    flush_para()
    close_list()
    return "\n".join(out)


# ---- diff rendering --------------------------------------------------------


def render_unified_diff(old: str, new: str, *, context: int = 2) -> str:
    """Render a unified diff as HTML. Empty string if no differences."""
    if old == new:
        return ""
    old_lines = old.splitlines(keepends=False)
    new_lines = new.splitlines(keepends=False)
    diff = list(
        difflib.unified_diff(old_lines, new_lines, n=context, lineterm="")
    )
    if not diff:
        return ""
    out = ["<pre class=\"diff\">"]
    for line in diff[2:]:  # skip the --- / +++ header lines
        if line.startswith("@@"):
            out.append(f"<span class=\"hunk\">{html.escape(line)}</span>")
        elif line.startswith("+"):
            out.append(f"<span class=\"add\">{html.escape(line)}</span>")
        elif line.startswith("-"):
            out.append(f"<span class=\"del\">{html.escape(line)}</span>")
        else:
            out.append(f"<span class=\"ctx\">{html.escape(line)}</span>")
    out.append("</pre>")
    return "\n".join(out)


# ---- build -----------------------------------------------------------------


def load_drugs() -> list[dict]:
    return yaml.safe_load(DRUGS_FILE.read_text())["drugs"]


def collect_versions(slug: str) -> list[Version]:
    """All committed versions for a drug, oldest first; falls back to working tree."""
    rel_dir = f"data/{slug}"
    commits = commits_for_path(rel_dir)
    versions: list[Version] = []
    seen_keys: set[tuple] = set()
    for c in commits:
        v = load_version_from_commit(slug, c)
        if v is None:
            continue
        # Dedupe consecutive commits where neither meta nor any section changed.
        key = (
            v.meta.get("spl_version"),
            v.meta.get("published_date"),
            tuple(v.sections.values()),
        )
        if seen_keys and key == max(seen_keys, default=None, key=lambda _: 0):
            continue
        seen_keys.add(key)
        versions.append(v)

    # If working-tree meta is newer than the last committed version, append it
    # so the local-dev preview shows pending changes.
    working = load_version_from_disk(slug)
    if working is not None and (
        not versions
        or working.meta.get("spl_version") != versions[-1].meta.get("spl_version")
        or any(working.sections[s] != versions[-1].sections[s] for s in SECTION_ORDER)
    ):
        versions.append(working)
    return versions


def changes_between(a: Version, b: Version, drug_slug: str, drug_name: str) -> list[Change]:
    out: list[Change] = []
    for s in SECTION_ORDER:
        diff_html = render_unified_diff(a.sections.get(s, ""), b.sections.get(s, ""))
        if not diff_html:
            continue
        out.append(Change(
            drug_slug=drug_slug,
            drug_name=drug_name,
            section_slug=s,
            section_label=SECTION_LABELS[s],
            from_version=a.meta.get("spl_version"),
            to_version=b.meta.get("spl_version"),
            when=b.commit.date,
            diff_html=diff_html,
        ))
    return out


def build():
    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True)
    (SITE_DIR / "drugs").mkdir()
    (SITE_DIR / "static").mkdir()

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md"] = md_to_html

    drugs = load_drugs()

    drug_summaries: list[dict] = []
    all_changes: list[Change] = []

    for drug in drugs:
        slug = drug["slug"]
        versions = collect_versions(slug)
        if not versions:
            continue

        latest = versions[-1]
        # Per-drug changes: pairwise diffs between consecutive versions.
        per_drug_changes: list[Change] = []
        for a, b in zip(versions, versions[1:]):
            per_drug_changes.extend(changes_between(a, b, slug, drug["name"]))

        all_changes.extend(per_drug_changes)

        # Render per-drug page.
        rendered_sections = [
            {
                "slug": s,
                "label": SECTION_LABELS[s],
                "html": md_to_html(latest.sections.get(s, "")),
                "empty": "_(not present in this label)_" in latest.sections.get(s, ""),
            }
            for s in SECTION_ORDER
        ]
        out_path = SITE_DIR / "drugs" / slug / "index.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(env.get_template("drug.html").render(
            drug=drug,
            meta=latest.meta,
            sections=rendered_sections,
            changes=sorted(per_drug_changes, key=lambda c: c.when, reverse=True),
            versions_count=len(versions),
            updated_at=latest.commit.date,
        ))

        drug_summaries.append({
            "slug": slug,
            "name": drug["name"],
            "title": latest.meta.get("title", drug["name"]),
            "spl_version": latest.meta.get("spl_version"),
            "published_date": latest.meta.get("published_date"),
            "updated_at": latest.commit.date,
            "change_count": len(per_drug_changes),
            "dailymed_url": latest.meta.get("dailymed_url"),
        })

    all_changes.sort(key=lambda c: c.when, reverse=True)
    now = datetime.now(timezone.utc)

    (SITE_DIR / "index.html").write_text(env.get_template("index.html").render(
        drugs=sorted(drug_summaries, key=lambda d: d["name"].lower()),
        recent_changes=all_changes[:10],
        generated_at=now,
    ))
    (SITE_DIR / "changes.html").write_text(env.get_template("changes.html").render(
        changes=all_changes,
        generated_at=now,
    ))
    (SITE_DIR / "feed.xml").write_text(env.get_template("feed.xml").render(
        changes=all_changes[:50],
        generated_at=now,
    ))
    shutil.copy(TEMPLATES_DIR / "styles.css", SITE_DIR / "static" / "styles.css")

    print(f"built {len(drug_summaries)} drug pages, {len(all_changes)} change entries")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
