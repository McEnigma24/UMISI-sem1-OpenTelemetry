#!/usr/bin/env bash
# 1) If ./out is not empty, lists it and asks; then clears ./out.
# 2) Mirror entire ./in into ./out (same folder tree and files).
# 3) Under ./out, find chunk files (path/to/stem.<digits>), join each group in place
#    to path/to/stem, then delete those chunk files under ./out.
#
# Optional: -r / --remove-input — after everything succeeds, delete all contents under ./in
# (empty dirs removed); ./out keeps the full result.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IN="${SCRIPT_DIR}/in"
OUT="${SCRIPT_DIR}/out"

usage() {
  cat <<'EOF'
Usage: combiner.sh [-r|--remove-input] [-h|--help]

  If ./out is not empty, lists it and asks for confirmation, then clears ./out.
  Mirrors ./in into ./out, then joins *.<digits> chunk groups inside ./out and removes
  the chunk files there. Non-chunk files remain as copied.

  -r, --remove-input  After success, remove everything under ./in (result only in ./out).
  -h, --help          Show this help.
EOF
}

REMOVE_INPUT=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    -r | --remove-input) REMOVE_INPUT=true ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1 (try --help)" >&2
      exit 1
      ;;
  esac
  shift
done

mkdir -p "$OUT"

if [[ -e "$IN" && ! -d "$IN" ]]; then
  echo "$IN istnieje, ale to nie jest katalog." >&2
  exit 1
fi
if [[ ! -d "$IN" ]]; then
  mkdir -p "$IN"
  echo "Brakowało katalogu $IN — utworzono. Wrzuć dane i uruchom ponownie." >&2
  exit 1
fi

if [[ -n "$(find "$OUT" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "W $OUT są jeszcze stare pliki:" >&2
  ls -la "$OUT" >&2 || true
  if [[ -t 0 ]]; then
    read -r -p "Usunąć całą zawartość $OUT i kontynuować? [t/N] " ans
    case "$ans" in t|T|tak|TAK|y|Y) ;;
      *) echo "Przerwano." >&2; exit 1 ;;
    esac
  else
    echo "$OUT nie jest pusty; bez interaktywnego terminala nie kasuję. Opróżnij $OUT lub uruchom w terminalu." >&2
    exit 1
  fi
fi
find "$OUT" -mindepth 1 -delete 2>/dev/null || true

# --- Mirror: IN → OUT ---
cp -a "${IN}/." "${OUT}/"

declare -A seen_stem
while IFS= read -r -d '' path; do
  [[ -f "$path" ]] || continue
  rel="${path#"$OUT"/}"
  [[ "$rel" =~ ^(.+)\.[0-9]+$ ]] || continue
  seen_stem["${BASH_REMATCH[1]}"]=1
done < <(find "$OUT" -type f -print0 2>/dev/null)

if [[ ${#seen_stem[@]} -eq 0 ]]; then
  echo "No chunk files under $OUT (mirrored $IN → $OUT only)." >&2
  if [[ "$REMOVE_INPUT" == true ]]; then
    find "$IN" -mindepth 1 -delete 2>/dev/null || true
    echo "Removed contents under $IN (--remove-input)." >&2
  fi
  exit 0
fi

shopt -s extglob nullglob
for stem_rel in "${!seen_stem[@]}"; do
  chunks=("$OUT/${stem_rel}".+([0-9]))
  if [[ ${#chunks[@]} -eq 0 ]]; then
    echo "No chunks for stem: $stem_rel" >&2
    exit 1
  fi
  mapfile -t sorted_chunks < <(
    for c in "${chunks[@]}"; do
      b=$(basename "$c")
      suf="${b##*.}"
      printf '%s|%s\n' "$suf" "$c"
    done | sort -t'|' -k1,1n | cut -d'|' -f2-
  )
  out_path="$OUT/$stem_rel"
  mkdir -p "$(dirname "$out_path")"
  cat "${sorted_chunks[@]}" >"$out_path"
  rm -f "${sorted_chunks[@]}"
  echo "Joined ${#sorted_chunks[@]} parts in place -> $out_path"
done
shopt -u extglob nullglob

if [[ "$REMOVE_INPUT" == true ]]; then
  find "$IN" -mindepth 1 -delete
  echo "Removed contents under $IN (--remove-input)." >&2
fi
