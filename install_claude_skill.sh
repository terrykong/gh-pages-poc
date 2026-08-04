#!/usr/bin/env bash
#
# install_claude_skill.sh
#
# Clones this repo to ~/gh-pages-poc (if missing) and installs the
# /upload-public-html Claude Code skill.
#
set -euo pipefail

REPO_URL="https://github.com/terrykong/gh-pages-poc.git"
REPO_DIR="$HOME/gh-pages-poc"
SKILL_DIR="$HOME/.claude/commands"
SKILL_FILE="$SKILL_DIR/upload-public-html.md"

# --- 1. Repo -----------------------------------------------------------------
if [[ -d "$REPO_DIR/.git" ]]; then
  echo "  Repo already present at $REPO_DIR"
else
  echo "  Cloning $REPO_URL -> $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
fi

# --- 2. Skill ----------------------------------------------------------------
mkdir -p "$SKILL_DIR"

cat > "$SKILL_FILE" << 'SKILL_EOF'
---
description: Upload an HTML file to the gh-pages-poc GitHub Pages site (PUBLIC)
---

# /upload-public-html

Upload an HTML visualization to the `terrykong/gh-pages-poc` GitHub Pages site.

## ⚠️ THIS SITE IS PUBLIC — READ FIRST

Unlike `nemo-html` (internal GitLab), **this repo and everything it serves are
public to the entire internet.** There is no authentication on the published site.

GitHub Pages cannot be made private here: on the Free plan Pages requires a public
repo, and even on Pro/Team a Pages site built from a private repo is *still served
publicly*. Access-controlled Pages is a GitHub Enterprise Cloud feature only.

**Before uploading anything, confirm the content contains none of:**
- Internal hostnames, cluster names, or cloud provider / datacenter locations
  (e.g. `hsg`, `oci-hsg`, `cw-dfw`, `coreweave`). Refer to hardware by **GPU SKU
  only** — `oci-hsg` → "GB200", `cw-dfw` → "H100".
- Unreleased model names, internal metrics, roadmap or revenue data
- Internal URLs (`*.nvidia.com` intranet, GitLab-master links), ticket IDs
- Colleagues' names, emails, or Slack/GitHub handles
- Credentials, tokens, or absolute paths revealing internal infra

**If you are uploading on someone's behalf and are not certain the content is
cleared for public release, STOP and ask them to confirm explicitly.** When in
doubt, use `/upload-nemo-html` (internal GitLab Pages) instead.

## Instructions

### 1. Use the repo at ~/gh-pages-poc

If it doesn't exist, clone it:
```bash
git clone https://github.com/terrykong/gh-pages-poc.git ~/gh-pages-poc
```

Pull latest before making changes:
```bash
git -C ~/gh-pages-poc pull --ff-only
```

### 2. Choose the destination directory

Determine the username: run `git config user.email` and take the part before `@`
(e.g. `terryk@nvidia.com` → `terryk`). If unset, or if `whoami` returns `root`
(you're likely in a container), ask the user for their username.

Files go in `~/gh-pages-poc/$USERNAME/`. Directory structure is preserved on the
published site, so `terryk/foo.html` serves at `/gh-pages-poc/terryk/foo.html`.

```bash
mkdir -p ~/gh-pages-poc/$USERNAME
```

### 3. Write the HTML

The user will either provide a path to an existing HTML file (copy it in), or ask
you to generate content (write it directly).

**When you GENERATE HTML, include the light/dark theme toggle** (default dark) —
see below.

Re-check the content against the public-content checklist above before continuing.

### 4. Commit and push

```bash
cd ~/gh-pages-poc
git add "$USERNAME"
git commit -s -m "Add <filename>"
git pull --rebase
git push
```

The `.github/workflows/pages.yml` workflow runs automatically on push to `main`.

### 5. Verify the page is live, then share the URL

GitHub Pages rebuilds **asynchronously** (Actions run + CDN propagation, typically
~30–90s), so the URL 404s — or serves the *old* version — for a bit. **Always poll
until it serves the new content before telling the user to look at it.** Never hand
over a link that 404s or still shows the previous version.

First confirm the workflow itself succeeded — a failed build means the page will
never update, and polling would spin pointlessly:

```bash
gh run list --repo terrykong/gh-pages-poc --limit 1
# or block until it finishes:
gh run watch "$(gh run list --repo terrykong/gh-pages-poc --limit 1 --json databaseId -q '.[0].databaseId')" \
  --repo terrykong/gh-pages-poc --exit-status
```

Then poll for HTTP 200 **and** a string unique to this update, so you catch stale
cached versions and not just 404s:

```bash
URL="https://terrykong.github.io/gh-pages-poc/$USERNAME/<filename>.html"
NEEDLE='a string that only appears in the new version'   # e.g. a new heading or caption
for i in $(seq 1 15); do
  body=$(curl -s "$URL")
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL")" = "200" ] && printf '%s' "$body" | grep -qF "$NEEDLE"; then
    echo "live ✓ (attempt $i)"; break
  fi
  echo "attempt $i: not live yet"; sleep 8
done
```

If you also uploaded images/assets, `curl -s -o /dev/null -w '%{http_code}'` each of
them and confirm `200` too. Only after the page (and any assets) verify live, print
the hosted URL for the user. (These checks need outbound network; if your shell
sandboxes network, run them with network access enabled.)

The site index at https://terrykong.github.io/gh-pages-poc/ lists every HTML and PDF
in the repo, sorted by last commit date — the new file appears there automatically.

## Light/dark theme toggle (include in every generated HTML)

Every HTML you GENERATE should support a light/dark toggle (default **dark**). The
clean way: drive **all** colors from CSS custom properties on `:root`, override them
under `:root[data-theme="light"]`, and add a small fixed toggle button that flips
`data-theme` on `<html>` and persists to `localStorage`. SVG/canvas that reference
`var(--text)` re-theme **live**; any element with a hardcoded dark background
(code/terminal blocks) needs an explicit light override so its text stays readable
on white.

**In `<style>` — define both themes and drive colors from the vars:**
```css
:root{ /* dark (default) */
  --bg:#0d1117; --surface:#161b22; --border:#30363d; --text:#e6edf3; --dim:#8b949e; --accent:#58a6ff;
}
:root[data-theme="light"]{ /* white background, dark text */
  --bg:#ffffff; --surface:#f6f8fa; --border:#d0d7de; --text:#1f2328; --dim:#59636e; --accent:#0969da;
}
body{background:var(--bg);color:var(--text)}   /* drive ALL colors from var(--…) */
#theme-toggle{position:fixed;top:14px;right:16px;z-index:200;background:var(--surface);
  border:1px solid var(--border);color:var(--text);border-radius:20px;padding:6px 12px;
  font:13px/1 'JetBrains Mono',monospace;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.18)}
/* hardcoded-dark elements (code blocks, etc.) need an explicit light override: */
:root[data-theme="light"] pre.cmd{background:#f6f8fa;color:#1f2328}
```

**Just before `</head>` — apply the saved theme before first paint (avoids a flash):**
```html
<script>(function(){try{if(localStorage.getItem('gh-pages-theme')==='light')
  document.documentElement.setAttribute('data-theme','light');}catch(e){}})();</script>
```

**Just after `<body>` — the toggle button + wiring:**
```html
<button id="theme-toggle" aria-label="Toggle light/dark theme">🌙 dark</button>
<script>(function(){var r=document.documentElement,K='gh-pages-theme';
  function lbl(){var b=document.getElementById('theme-toggle');if(b)b.textContent=r.getAttribute('data-theme')==='light'?'☀ light':'🌙 dark';}
  function wire(){var b=document.getElementById('theme-toggle');if(!b)return;
    b.onclick=function(){if(r.getAttribute('data-theme')==='light'){r.removeAttribute('data-theme');try{localStorage.setItem(K,'dark')}catch(e){}}
      else{r.setAttribute('data-theme','light');try{localStorage.setItem(K,'light')}catch(e){}}lbl();};lbl();}
  document.readyState!=='loading'?wire():document.addEventListener('DOMContentLoaded',wire);})();</script>
```

Guidance:
- Drive **every** color from `var(--…)` so the toggle is automatic.
- For SVG/canvas charts, set text fill to `var(--text)` so it re-resolves live on toggle.
- Any hardcoded dark background (terminal/code blocks) needs a
  `:root[data-theme="light"] …` override so its text stays readable on white.
- Validate the inline JS parses before uploading (e.g. `esprima.parseScript`); a
  syntax error there blanks the whole page.
SKILL_EOF

echo ""
echo "  Repo:  $REPO_DIR"
echo "  Skill: $SKILL_FILE"
echo "  Use it with: /upload-public-html"
echo "  Site:  https://terrykong.github.io/gh-pages-poc/"
echo ""
echo "  NOTE: this site is PUBLIC to the internet. Do not upload internal content."
echo ""
echo "Setup complete!"
