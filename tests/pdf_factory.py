"""Builds small, valid PDFs with one text line per page, so tests don't need binary fixtures."""


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    """Each page is a list of text lines, drawn top to bottom in Helvetica. An empty list is a blank page."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_obj = 3 + 2 * n
    for i, lines in enumerate(pages):
        content_obj = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_obj} 0 R "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> >>".encode()
        )
        ops = "".join(f"BT /F1 11 Tf 50 {740 - 16 * j} Td ({_esc(line)}) Tj ET\n" for j, line in enumerate(lines))
        stream = ops.encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def handbook_page(topic: str, label: str, words: int = 120) -> list[str]:
    """A page with enough text to chunk, ending in a printed page number like the Airframe handbook."""
    body = [f"{topic} inspection procedure step {k} torque safety wire" for k in range(words // 8)]
    return [*body, label]
