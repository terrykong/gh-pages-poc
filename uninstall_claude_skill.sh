#!/usr/bin/env bash
#
# uninstall_claude_skill.sh
#
# Removes the /upload-public-html Claude Code skill.
# With --repo, also removes the ~/gh-pages-poc clone (refuses if it has
# uncommitted or unpushed work, unless --force).
#
set -euo pipefail

REPO_DIR="$HOME/gh-pages-poc"
SKILL_FILE="$HOME/.claude/commands/upload-public-html.md"

REMOVE_REPO=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --repo)  REMOVE_REPO=1 ;;
    --force) FORCE=1 ;;
    -h|--help)
      echo "Usage: $0 [--repo] [--force]"
      echo "  (no args)  remove the /upload-public-html skill only"
      echo "  --repo     also remove the $REPO_DIR clone"
      echo "  --force    with --repo, remove even if it has uncommitted/unpushed work"
      exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --- Skill -------------------------------------------------------------------
if [[ -f "$SKILL_FILE" ]]; then
  echo "This will remove the /upload-public-html Claude Code skill:"
  echo "  $SKILL_FILE"
else
  echo "Skill not found at $SKILL_FILE — nothing to remove there."
fi

# --- Repo safety checks ------------------------------------------------------
if [[ "$REMOVE_REPO" == 1 ]]; then
  if [[ ! -d "$REPO_DIR" ]]; then
    echo "Repo not found at $REPO_DIR — nothing to remove there."
    REMOVE_REPO=0
  else
    dirty=""
    unpushed=""
    if [[ -d "$REPO_DIR/.git" ]]; then
      [[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]] && dirty=1
      # Commits on the current branch not present on its upstream.
      if upstream=$(git -C "$REPO_DIR" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null); then
        [[ -n "$(git -C "$REPO_DIR" log --oneline "$upstream"..HEAD 2>/dev/null)" ]] && unpushed=1
      else
        unpushed=1   # no upstream at all — treat as unpushed
      fi
    else
      dirty=1        # not a git repo; can't verify, so treat as unsafe
    fi

    if [[ -n "$dirty$unpushed" && "$FORCE" != 1 ]]; then
      echo ""
      echo "REFUSING to delete $REPO_DIR — it has work that isn't on the remote:"
      [[ -n "$dirty" ]]    && echo "  * uncommitted changes (or not a git repo)"
      [[ -n "$unpushed" ]] && echo "  * commits not pushed to upstream"
      echo ""
      echo "Push or stash your work first, or re-run with --force to delete anyway."
      exit 1
    fi
    echo "This will DELETE the repo clone:"
    echo "  $REPO_DIR"
    [[ "$FORCE" == 1 ]] && echo "  (--force: skipping the uncommitted/unpushed check)"
  fi
fi

if [[ ! -f "$SKILL_FILE" && "$REMOVE_REPO" == 0 ]]; then
  exit 0
fi

echo ""
read -p "Press Enter to continue, or Ctrl+C to cancel... "

[[ -f "$SKILL_FILE" ]] && rm "$SKILL_FILE" && echo "Removed $SKILL_FILE"
[[ "$REMOVE_REPO" == 1 ]] && rm -rf "$REPO_DIR" && echo "Removed $REPO_DIR"

echo "Done. The /upload-public-html skill is no longer available."
