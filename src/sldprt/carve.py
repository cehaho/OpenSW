"""Find and inflate compressed regions of a blob."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .container import SECTION_MAGIC

ZLIB_HEADERS = (b"\x78\x01", b"\x78\x9c", b"\x78\xda", b"\x78\x5e")


@dataclass
class Region:
    kind: str
    offset: int
    data: bytes
    header: dict[str, int]

    @property
    def size(self) -> int:
        return len(self.data)


def carve_sections(blob: bytes) -> list[Region]:
    regions: list[Region] = []
    pos = blob.find(SECTION_MAGIC)
    while pos != -1:
        at = pos + len(SECTION_MAGIC)

        end = len(blob)
        if pos >= 4:
            declared = struct.unpack_from("<I", blob, pos - 4)[0]
            if 0 < declared <= len(blob) - pos + 4:
                end = min(end, pos - 4 + 4 + declared)

        parts: list[bytes] = []
        members = 0
        while at + 8 <= end:
            uncomp_sz, comp_sz = struct.unpack_from("<II", blob, at)
            if uncomp_sz == 0 and comp_sz == 0:
                at += 8
                break
            if comp_sz == 0 or at + 8 + comp_sz > end:
                break
            try:
                raw = zlib.decompress(blob[at + 8 : at + 8 + comp_sz])
            except zlib.error:
                break
            if len(raw) != uncomp_sz:
                break
            parts.append(raw)
            members += 1
            at += 8 + comp_sz

        if parts:
            regions.append(
                Region(
                    kind="section",
                    offset=pos,
                    data=b"".join(parts),
                    header={
                        "members": members,
                        "uncompressed": sum(len(p) for p in parts),
                        "consumed": at - pos,
                    },
                )
            )
            pos = blob.find(SECTION_MAGIC, at)
            continue
        pos = blob.find(SECTION_MAGIC, pos + 1)
    return regions


def carve_zlib(blob: bytes, min_size: int = 64, limit: int = 64) -> list[Region]:
    regions: list[Region] = []
    consumed_until = 0
    pos = 0
    while pos < len(blob) and len(regions) < limit:
        nxt = min(
            (p for p in (blob.find(h, pos) for h in ZLIB_HEADERS) if p != -1),
            default=-1,
        )
        if nxt == -1:
            break
        if nxt < consumed_until:
            pos = nxt + 1
            continue
        obj = zlib.decompressobj()
        try:
            raw = obj.decompress(blob[nxt:])
        except zlib.error:
            raw = b""
        if len(raw) >= min_size:
            used = len(blob) - nxt - len(obj.unused_data)
            regions.append(
                Region(
                    kind="zlib",
                    offset=nxt,
                    data=raw,
                    header={
                        "actual_uncompressed": len(raw),
                        "actual_compressed": used,
                    },
                )
            )
            consumed_until = nxt + used
            pos = consumed_until
        else:
            pos = nxt + 1
    return regions


def carve(blob: bytes, *, scan_bare: bool = True) -> list[Region]:
    regions = carve_sections(blob)
    if not scan_bare:
        return regions
    covered = [
        (r.offset, r.offset + r.header.get("consumed", 0)) for r in regions
    ]
    for region in carve_zlib(blob):
        if any(start <= region.offset < end for start, end in covered):
            continue
        regions.append(region)
    return sorted(regions, key=lambda r: r.offset)

