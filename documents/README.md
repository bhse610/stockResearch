# Source documents for quarterly analysis

The quarterly analysis prompt (`prompts/QUARTERLYANALYSIS.md`) is **quote-first**:
every factual claim must be backed by an exact quote from a source document,
with the document name cited. Place your source documents here.

## Layout

```
documents/
    README.md                  # this file (ignored: not .md analysis input? see note)
    annual-report-2025.pdf     # GLOBAL doc -> applied to EVERY company
    INFY/                      # per-company folder -> applied to INFY only
        2025-Q2-earnings.md
        2025-Q1-earnings.md
    HDFCBANK/
        guidance.md
```

- **Global documents** (files directly in `documents/`) are included for every
  company.
- **Per-company documents** (files under `documents/<SYMBOL>/`) are included only
  for that company. The folder name should match the holding symbol
  (case-insensitive), e.g. `INFY`, `HDFCBANK`.

## Supported formats

| Extension      | Notes                                   |
|----------------|-----------------------------------------|
| `.md`, `.txt`  | Read as UTF-8 text                      |
| `.json`, `.csv`| Read as text (quote from raw content)   |
| `.pdf`         | Extracted via `pypdf` (if installed)    |

Install PDF support with:

```powershell
pip install pypdf
```

> Note: `README.md` in this folder is read like any other `.md` file. If you
> don't want it sent to the model, move your documents into per-company folders
> only, or rename this file so it isn't picked up.
