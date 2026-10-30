"""Text cleanup shared by every source."""

import re

# Control characters other than \t \n \r. GPO formatting artifacts put NULs in 276 of 2,545 AD texts, and
# Postgres text can't hold NUL (DATA.md "AD text quirks").
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Private-use code points: symbol-font glyphs that PDFium extracts as U+F8E7 and similar (seen in
# AC 43.13-1B: "8-39.\uf8e78-44."). They carry no meaning in plain text.
PRIVATE_USE = re.compile("[\ue000-\uf8ff]")


# Hyphenation marks inside words. PDFium returns U+FFFE (a non-character) where the PDF breaks a word across
# lines ("ca\ufffeble", "re\ufffegardless"), and some PDFs carry soft hyphens (U+00AD). Left in, they split
# words for full-text search, embeddings and quote checks. Found in 76 % of AC 43.13-1B chunks (issue #9).
HYPHENATION = re.compile("[\ufffe\u00ad]")


def clean(text: str) -> str:
    return PRIVATE_USE.sub(" ", CONTROL.sub("", HYPHENATION.sub("", text)))
