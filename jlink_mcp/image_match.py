"""Read-only evidence comparing immutable ELF load bytes to profiled Flash."""
import hashlib


def compare_image(symbols, profile, read):
    sections, skipped = symbols.readonly_sections()
    skipped = list(skipped)
    ranges, errors = [], []
    compared, mismatches, eligible = 0, 0, sum(s.get("size", 0) for s in skipped)
    flash = [r for r in profile.regions if r.kind == "flash" and not r.mutable]
    if sum(len(s["data"]) for s in sections) > 16 * 1024 * 1024:
        raise ValueError("ELF comparison exceeds 16 MiB")
    for section in sections:
        base, data = section["address"], section["data"]
        eligible += len(data)
        covered = 0
        for region in flash:
            start, end = max(base, region.address), min(base + len(data), region.address + region.size)
            if start >= end:
                continue
            covered += end - start
            expected = data[start - base:end - base]
            record = {"section": section["name"], "address": start, "size": end - start,
                      "expected_sha256": hashlib.sha256(expected).hexdigest()}
            digest, different, first, read_size = hashlib.sha256(), 0, [], 0
            try:
                for offset in range(0, len(expected), 65536):
                    want = expected[offset:offset + 65536]
                    actual = read(start + offset, len(want))
                    if len(actual) != len(want):
                        raise ValueError("Short read during image comparison")
                    digest.update(actual)
                    read_size += len(actual)
                    for index, (a, b) in enumerate(zip(actual, want)):
                        if a != b:
                            different += 1
                            if len(first) < 16:
                                first.append({"address": start + offset + index, "expected": b, "actual": a})
                record.update(actual_sha256=digest.hexdigest(), matched=different == 0,
                              mismatched_bytes=different, first_mismatches=first)
            except Exception as exc:
                record.update(matched=None, error=str(exc), mismatched_bytes=different)
                errors.append({"address": start, "error": str(exc)})
            compared += read_size
            mismatches += different
            ranges.append(record)
        if covered != len(data):
            skipped.append({"section": section["name"], "size": len(data) - covered,
                            "reason": "outside immutable Flash regions (including mutable parameter regions)"})
    status = "mismatch" if mismatches else "unknown" if errors or not compared else "partial" if skipped else "matched"
    return {"success": status == "matched", "status": status, "compared_bytes": compared,
            "eligible_bytes": eligible, "mismatched_bytes": mismatches, "ranges": ranges,
            "skipped": skipped, "errors": errors, "elf_sha256": symbols.sha256,
            "scope": "ELF allocated read-only PROGBITS at Flash load addresses; not whole Flash or build provenance"}
