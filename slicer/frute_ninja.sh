#!/usr/bin/env bash
# Recursively split every regular file under ./in into ./out, mirroring subdirs.
# Chunks: <relpath/to/file>.<digits> — digit count is minimal for the part count (split -a).
# Skips files whose basename looks like a chunk (trailing .digits).
# Chunk size is fixed at 100M (same as: split -b 100M, i.e. 100 * 1024^2 bytes).
# If ./out is not empty, lists it and asks before clearing; then only output from this run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IN="${SCRIPT_DIR}/in"
OUT="${SCRIPT_DIR}/out"
# Match GNU split -b 100M (binary megabytes)
CHUNK_BYTES=$((100 * 1024 * 1024))

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

# Licz pliki do cięcia (bez kasowania out, jeśli i tak nic nie zrobimy)
split_count=0
while IFS= read -r -d '' path; do
  [[ -f "$path" ]] || continue
  rel="${path#"$IN"/}"
  base="$(basename "$rel")"
  [[ "$base" =~ \.[0-9]+$ ]] && continue
  split_count=$((split_count + 1))
done < <(find "$IN" -type f -print0 2>/dev/null)

if [[ "$split_count" -eq 0 ]]; then
  echo "Nic do cięcia pod $IN (brak plików albo tylko nazwy typu chunków)." >&2
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

split_count=0
while IFS= read -r -d '' path; do
  [[ -f "$path" ]] || continue
  rel="${path#"$IN"/}"
  base="$(basename "$rel")"
  # Do not re-split chunk parts
  [[ "$base" =~ \.[0-9]+$ ]] && continue

  dir_rel="$(dirname "$rel")"
  if [[ "$dir_rel" == "." ]]; then
    out_dir="$OUT"
  else
    out_dir="$OUT/$dir_rel"
  fi
  mkdir -p "$out_dir"

  size="$(stat -c%s "$path")"
  parts=$(( (size + CHUNK_BYTES - 1) / CHUNK_BYTES ))
  [[ "$parts" -lt 1 ]] && parts=1 # e.g. empty file

  suffix_digits=1
  max_ids=10
  while (( parts > max_ids )); do
    suffix_digits=$((suffix_digits + 1))
    max_ids=$((max_ids * 10))
  done

  echo "Split: $rel — $parts part(s), using $suffix_digits suffix digit(s) (100M each)"
  if [[ "$size" -eq 0 ]]; then
    # GNU split writes no outputs for empty input; one zero-byte chunk matches 1 part / 1 digit.
    : >"$out_dir/${base}.$(printf '%0*d' "$suffix_digits" 0)"
  else
    split -b 100M -d -a "$suffix_digits" "$path" "$out_dir/${base}."
  fi
  split_count=$((split_count + 1))
done < <(find "$IN" -type f -print0 2>/dev/null)
