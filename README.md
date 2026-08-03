# gh-pages-poc

Proof of concept: serve arbitrary static HTML from a GitHub repo via GitHub Pages,
mirroring the workflow of the internal `nemo-html` GitLab Pages repo.

## How it works

`.github/workflows/pages.yml` runs on every push to `main`:

1. Copies every `.html` / `.pdf` / image / `.css` / `.js` / `.json` / `.csv` in the repo
   into `public/`, preserving directory structure.
2. Generates `public/index.html` — a table of all HTML and PDF files sorted by their
   last commit date (newest first).
3. Publishes `public/` to GitHub Pages via `actions/upload-pages-artifact` +
   `actions/deploy-pages`.

## Usage

Drop an HTML file anywhere in the repo, commit, push to `main`. It appears on the
site at the same relative path, and in the index.

## Note on visibility

GitHub Pages requires a **public** repo on the Free plan. Even on Pro/Team, a Pages
site built from a private repo is still served publicly — access-controlled Pages is
a GitHub Enterprise Cloud feature. Do not put anything sensitive here.
