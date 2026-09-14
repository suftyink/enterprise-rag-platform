def make_pdf(pages: list[str]) -> bytes:
    """Build a small text PDF without adding a test-only PDF dependency."""

    page_count = len(pages)
    font_id = 3 + page_count
    content_start = font_id + 1
    page_ids = list(range(3, 3 + page_count))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{value} 0 R' for value in page_ids)}] "
            f"/Count {page_count} >>"
        ).encode(),
    ]
    for index, page_id in enumerate(page_ids):
        content_id = content_start + index
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        assert page_id == len(objects)
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for text in pages:
        commands = [b"BT /F1 12 Tf 14 TL 72 720 Td"]
        for line in text.splitlines() or [""]:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped}) Tj T*".encode("latin-1"))
        commands.append(b"ET")
        stream = b"\n".join(commands)
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_id, value in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{object_id} 0 obj\n".encode() + value + b"\nendobj\n")
    xref_offset = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode()
    )
    return bytes(data)
