# FDA Label Watch

Daily snapshots and human-readable diffs of FDA drug labels.

Drug labels change quietly: a new boxed warning, a tightened contraindication, a
new adverse reaction. The official source ([DailyMed](https://dailymed.nlm.nih.gov/))
publishes every revision but does not show you what changed between versions.
This project does.

Each day a GitHub Action fetches the current label for every tracked drug,
extracts the high-signal sections (boxed warnings, indications,
contraindications, warnings & precautions, adverse reactions, drug interactions,
dosage, use in specific populations), and commits whatever changed. The commit
history *is* the version history. A static site rebuilds with inline diffs and
publishes to GitHub Pages.

## What you get

- A static site at `<your-pages-url>` listing every tracked drug with its latest
  version, published date, and a count of changes recorded so far.
- A per-drug page with the current label sections (collapsible) and a
  reverse-chronological list of every section change since tracking began,
  rendered as inline diffs.
- An Atom feed (`feed.xml`) of the most recent changes for subscribing.
- The full version history of every drug as plain markdown files in `data/`,
  browsable on GitHub.

## Repo layout

```
drugs.yaml                # tracked drugs (slug, name, DailyMed setid)
data/{slug}/              # one folder per drug
  meta.yaml               # latest version metadata
  boxed_warning.md        # plain-text snapshot of each tracked section
  indications.md
  ... (etc)
scripts/
  fetch.py                # download from DailyMed, write data/
  build_site.py           # walk git log, render site/
  bootstrap_setids.py     # find a setid for a new drug
  templates/              # jinja2 templates + styles.css
.github/workflows/
  daily.yml               # nightly fetch → commit → build → deploy
site/                     # generated, gitignored
```

## Run locally

Requires Python 3.10+.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Fetch latest snapshots (writes/updates files under data/)
python scripts/fetch.py

# Build the static site
python scripts/build_site.py

# Open it
open site/index.html      # macOS
xdg-open site/index.html  # Linux
```

To re-fetch even if the version hasn't changed:
```sh
python scripts/fetch.py --force
```

To work on a single drug:
```sh
python scripts/fetch.py --only ozempic --force
```

## Adding a drug

1. Find the DailyMed setid:
   ```sh
   python scripts/bootstrap_setids.py "your drug name"
   ```
   It prints up to 5 candidates per query. Pick the one whose manufacturer/label
   matches what you actually want to track (originator labels are usually most
   stable; some generics are repackagers and may move around).

2. Append the entry to `drugs.yaml`:
   ```yaml
     - slug: my-drug
       name: My Drug (active ingredient)
       setid: 00000000-0000-0000-0000-000000000000
       notes: optional one-liner
   ```

3. Open a PR. The next scheduled run picks it up automatically.

## Publishing

This repo is designed to deploy to GitHub Pages with zero configuration beyond:

1. Push to GitHub.
2. **Settings → Pages → Source: GitHub Actions.**
3. The first push triggers `daily.yml` (it runs on push to `main` as well as on
   the daily cron). After the workflow finishes, your site will be live at
   `https://<user>.github.io/fda-label-watch/`.

The workflow needs `contents: write` permission to commit new snapshots — that
is granted in the workflow file. No secrets required.

## How it works (a bit deeper)

- **Source.** [DailyMed](https://dailymed.nlm.nih.gov/), the NLM/NIH-hosted
  official mirror of FDA-approved labels. Each label has a stable Set ID;
  every revision increments `spl_version` and gets a new `published_date`.
- **Change detection.** Before downloading the full SPL XML, we hit the cheap
  `history.json` endpoint to see if the latest version differs from what we
  have on disk. Only changed labels are re-fetched.
- **Section extraction.** We parse the SPL XML, look up sections by LOINC code
  (e.g. `34066-1` = Boxed Warning, `43685-7` = Warnings & Precautions), and
  recurse into nested subsections. Output is normalized one-sentence-per-line
  so prose diffs read naturally.
- **History.** Git is the database. Each daily run produces at most one commit,
  named `snapshot: YYYY-MM-DD`. The static site walks `git log -- data/{slug}/`
  to compute pairwise diffs between consecutive commits.

## Limitations

- We track *one setid per drug*. Many generics have dozens of NDAs from
  different manufacturers; the originator label is usually most representative
  but not always identical to what a given pharmacy dispenses.
- Tables in the SPL XML are flattened to plain text; complex tables (e.g.
  pharmacokinetic data) may diff noisily.
- The first snapshot of any drug shows no diffs — they only appear once a
  second version exists. Useful history accrues over weeks and months, not
  immediately.
- This is not a regulatory or clinical decision tool. Always consult the
  authoritative label on DailyMed.

## License

MIT. See [LICENSE](LICENSE).
