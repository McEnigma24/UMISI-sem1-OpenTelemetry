#!/bin/bash

# FILES=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.(c|cpp)$') # Nokia
FILES=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.(c|cpp|h|hpp)$')

if [ -z "$FILES" ]; then
    echo "Brak plików do formatowania."
    exit 0
fi

echo "Formatowanie zmienionych plików za pomocą ClangFormat..."
for FILE in $FILES; do
    echo "$FILE"
    # clang-format -i "$FILE"        # Formatowanie pliku
    clang-format -i -style=file:"${PWD}/external/CORE_lib/.clang-format" "$FILE"
    git add "$FILE"                # Ponowne dodanie do stage po formatowaniu
done

echo "Pliki sformatowane i dodane do commita."
exit 0