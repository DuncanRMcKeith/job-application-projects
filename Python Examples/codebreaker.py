import requests
import re

def render_unicode_grid(google_doc_url: str):
    # Force Google Docs to return plain text
    if google_doc_url.endswith("/pub"):
        google_doc_url = google_doc_url + "?embedded=true&output=txt"
    else:
        google_doc_url = google_doc_url.replace("/pub?", "/pub?embedded=true&output=txt&")

    # Download the document
    response = requests.get(google_doc_url)
    response.raise_for_status()
    text = response.text

    # Normalize whitespace
    normalized = (
        text.replace("\u00A0", " ")  # non-breaking spaces
            .replace("\t", " ")      # tabs
    )
    normalized = re.sub(r"\s+", " ", normalized)

    # Parse lines
    pattern = re.compile(r"(.+?) (\d+) (\d+)")

    points = []
    max_x = 0
    max_y = 0

    for line in normalized.splitlines():
        line = line.strip()
        if not line:
            continue

        match = pattern.match(line)
        if not match:
            continue

        char, x, y = match.groups()
        x = int(x)
        y = int(y)

        points.append((char, x, y))
        max_x = max(max_x, x)
        max_y = max(max_y, y)

    # Build grid
    grid = [[" " for _ in range(max_x + 1)] for _ in range(max_y + 1)]

    # Place characters
    for char, x, y in points:
        grid[y][x] = char

    # Print grid
    for row in grid:
        print("".join(row))


if __name__ == "__main__":
    render_unicode_grid(
        "https://docs.google.com/document/d/e/2PACX-1vSvM5gDlNvt7npYHhp_XfsJvuntUhq184By5xO_pA4b_gCWeXb6dM6ZxwN8rE6S4ghUsCj2VKR21oEP/pub"
    )