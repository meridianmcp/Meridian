#!/usr/bin/env bash
# hooks/pre-commit.sh — credential scanner pre-commit hook
#
# Blocks commits containing embedded secrets: Postgres connection strings,
# Neon password tokens, API keys, and .env-style DB_URL assignments.
#
# Install (one-time per repo clone):
#   git config core.hooksPath hooks
#
# Or symlink manually:
#   ln -sf ../../hooks/pre-commit.sh .git/hooks/pre-commit
#
# Bypass (only when certain it's a false positive):
#   git commit --no-verify
set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
DIM='\033[0;90m'
NC='\033[0m'

# Credential patterns (ERE syntax for grep -E)
PATTERNS=(
    # Postgres URLs with embedded passwords
    'postgresql://[^:@]+:[^@]{6,}@'
    'postgres://[^:@]+:[^@]{6,}@'
    # Neon password tokens (npg_ prefix + 16+ chars)
    'npg_[a-zA-Z0-9]{16,}'
    # Neon DB URL env-var assignment
    'NEON_DB_URL[[:space:]]*=[[:space:]]*postgresql'
    # TOML db_url field with Postgres value
    'db_url[[:space:]]*=[[:space:]]*"postgresql'
    # Meridian API tokens
    'sk_meridian_[a-zA-Z0-9_]{20,}'
    # Stripe live keys
    'sk_live_[a-zA-Z0-9]{20,}'
    'rk_live_[a-zA-Z0-9]{20,}'
    # DATABASE_URL / DB_URL with embedded credentials
    'DATABASE_URL[[:space:]]*=[[:space:]]*postgresql://[^:@]+:[^@]{6,}@'
    'DB_URL[[:space:]]*=[[:space:]]*postgresql://[^:@]+:[^@]{6,}@'
)

# File extensions to skip (binary / generated)
SKIP_EXTENSIONS=('.png' '.jpg' '.jpeg' '.gif' '.ico' '.svg' '.woff' '.woff2'
                 '.ttf' '.eot' '.mp4' '.zip' '.tar' '.gz' '.lock' '.pyc')

staged_files=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
[[ -z "$staged_files" ]] && exit 0

hits=0

while IFS= read -r file; do
    # Skip by extension
    skip=0
    for ext in "${SKIP_EXTENSIONS[@]}"; do
        [[ "$file" == *"$ext" ]] && { skip=1; break; }
    done
    [[ $skip -eq 1 ]] && continue

    # Read staged content (not working tree)
    content=$(git show ":$file" 2>/dev/null) || continue

    for pattern in "${PATTERNS[@]}"; do
        matches=$(echo "$content" | grep -nE "$pattern" 2>/dev/null || true)
        if [[ -n "$matches" ]]; then
            if [[ $hits -eq 0 ]]; then
                echo ""
                echo -e "${RED}⛔  COMMIT BLOCKED — potential credentials in staged files${NC}"
                echo ""
            fi
            echo -e "  ${YELLOW}${file}${NC}  ${DIM}(pattern: ${pattern})${NC}"
            echo "$matches" | head -3 | while IFS= read -r line; do
                echo -e "    ${DIM}${line}${NC}"
            done
            echo ""
            hits=$((hits + 1))
        fi
    done
done <<< "$staged_files"

if [[ $hits -gt 0 ]]; then
    echo "  Remove credentials before committing."
    echo "  Use environment variables or a .env file (add .env to .gitignore)."
    echo ""
    echo -e "  ${DIM}Bypass only when certain: git commit --no-verify${NC}"
    echo ""
    exit 1
fi

exit 0
