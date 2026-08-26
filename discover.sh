#!/usr/bin/env bash
# Stage 1 (anonymous fallback): discover Instagram post/reel URLs for an account
# via search-engine indexes, since Instagram's own enumeration APIs require auth.
set -uo pipefail

USER_NAME="${1:-networkchuck}"
OUT="${2:-out/urls.txt}"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
RAW=$(mktemp)

queries=(
  "site:instagram.com ${USER_NAME}"
  "site:instagram.com/reel ${USER_NAME}"
  "site:instagram.com/p ${USER_NAME}"
  "instagram.com ${USER_NAME} reel"
  "\"${USER_NAME}\" instagram reel"
)

for q in "${queries[@]}"; do
  for offset in 0 30 60; do
    enc=$(printf '%s' "$q" | od -An -tx1 | tr ' ' '%' | tr -d '\n' | sed 's/%$//')
    curl -s --max-time 40 -A "$UA" -H "Accept-Language: en-US,en;q=0.9" \
      --data-urlencode "q=$q" --data "s=$offset" \
      "https://lite.duckduckgo.com/lite/" >> "$RAW" 2>/dev/null
    printf '.' >&2
  done
done
echo >&2

grep -o -E 'instagram\.com/(p|reel)/[A-Za-z0-9_-]{5,}' "$RAW" \
  | sed -E 's#.*/(p|reel)/#\1 #' | awk '{print $2}' | sort -u > "$RAW.codes"

echo "raw unique shortcodes found: $(wc -l < "$RAW.codes")" >&2
mkdir -p "$(dirname "$OUT")"
sed 's#^#https://www.instagram.com/p/#; s#$#/#' "$RAW.codes" > "$OUT"
rm -f "$RAW"
echo "wrote $OUT" >&2
