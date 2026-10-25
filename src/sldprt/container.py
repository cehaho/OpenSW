"""The two .SLDPRT containers, OLE2 and block stream, as named blobs."""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

OLE2_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
BLOCK_MARKER = bytes.fromhex("140006000800")

SECTION_MAGIC = bytes.fromhex("231dd571da8148a2a85898b21b89ef99")

BLOCK_HEADER_SIZE = 26


def nibble_swap(data: bytes, *, key: int = 4) -> bytes:
    shift = key & 7
    if shift == 0:
        return bytes(data)
    return bytes(((b << shift) & 0xFF) | (b >> (8 - shift)) for b in data)


@dataclass
class Blob:
    name: str
    data: bytes
    origin: str
    offset: int | None = None
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.data)


class ContainerError(Exception):
    pass


def detect_container(data: bytes) -> str:
    if data.startswith(OLE2_MAGIC):
        return "cfb"
    if BLOCK_MARKER in data[:4096]:
        return "block"
    if BLOCK_MARKER in data:
        return "block"
    return "unknown"


def read_cfb(path: Path, wanted: Callable[[str], bool] | None = None) -> list[Blob]:
    try:
        import olefile
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ContainerError(
            "reading a CFB .SLDPRT needs olefile (pip install olefile)"
        ) from exc

    blobs: list[Blob] = []
    with olefile.OleFileIO(str(path)) as ole:
        for parts in ole.listdir(streams=True, storages=False):
            name = "/".join(parts)
            if wanted is not None and not wanted(name):
                continue
            with ole.openstream(parts) as stream:
                blobs.append(Blob(name=name, data=stream.read(), origin="cfb"))
    return blobs


@dataclass
class MarkerSite:
    offset: int
    kind: str
    name: str | None = None
    end: int | None = None
    detail: dict[str, object] = field(default_factory=dict)


def _u32le(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _printable(name: str) -> bool:
    return bool(name) and all(0x20 <= ord(c) < 0x7F for c in name)


def _try_block(data: bytes, off: int, *, key: int = 4) -> MarkerSite | None:
    if off + BLOCK_HEADER_SIZE > len(data):
        return None
    type_id = _u32le(data, off + 6)
    crc = _u32le(data, off + 10)
    comp_sz = _u32le(data, off + 14)
    uncomp_sz = _u32le(data, off + 18)
    pre_sz = _u32le(data, off + 22)

    if pre_sz > 4096 or comp_sz > len(data) or uncomp_sz > (1 << 31):
        return None
    body = off + BLOCK_HEADER_SIZE
    end = body + pre_sz + comp_sz
    if end > len(data):
        return None

    payload = data[body + pre_sz : end]
    try:
        inflater = zlib.decompressobj(-15)
        raw = inflater.decompress(payload, uncomp_sz + 1)
    except zlib.error:
        return None
    if len(raw) != uncomp_sz or not inflater.eof or zlib.crc32(raw) != crc:
        return None

    name = nibble_swap(data[body : body + pre_sz], key=key).decode("latin-1")
    return MarkerSite(
        offset=off,
        kind="block",
        name=name if _printable(name) else None,
        end=end,
        detail={
            "type_id": type_id,
            "comp_sz": comp_sz,
            "uncomp_sz": uncomp_sz,
            "raw": raw,
        },
    )


NAME_OFFSETS = (BLOCK_HEADER_SIZE, 40)


def _try_index_entry(data: bytes, off: int, *, key: int = 4) -> MarkerSite | None:
    if off + BLOCK_HEADER_SIZE > len(data):
        return None
    type_id = _u32le(data, off + 6)
    first = _u32le(data, off + 10)
    second = _u32le(data, off + 14)
    third = _u32le(data, off + 18)
    name_len = _u32le(data, off + 22)
    if not 0 < name_len < 500:
        return None

    for name_at in NAME_OFFSETS:
        end = off + name_at + name_len
        if end > len(data):
            continue
        name = nibble_swap(data[off + name_at : end], key=key).decode("latin-1")
        if not _printable(name):
            continue
        is_cache = third != 0 and first == 2 * third and second == third // 2
        detail: dict[str, object] = {"name_at": name_at, "type_id": type_id}
        if is_cache:
            detail["L"] = third
        else:
            detail.update(checksum=first, comp_sz=second, uncomp_sz=third)
        return MarkerSite(
            offset=off,
            kind="cache_cell" if is_cache else "dir_entry",
            name=name,
            end=end,
            detail=detail,
        )
    return None


def name_key(data: bytes) -> int:
    if len(data) > 7 and data[4:7] == b"\x00\x00\x00" and data[7]:
        return data[7]
    return 4


def scan_markers(data: bytes) -> list[MarkerSite]:
    key = name_key(data)
    sites: list[MarkerSite] = []
    off = data.find(BLOCK_MARKER)
    while off != -1:
        site = (
            _try_block(data, off, key=key)
            or _try_index_entry(data, off, key=key)
            or MarkerSite(offset=off, kind="unknown")
        )
        sites.append(site)
        resume = site.end if site.kind == "block" else off + len(BLOCK_MARKER)
        off = data.find(BLOCK_MARKER, resume)
    return sites


def read_block_container(data: bytes) -> tuple[list[Blob], list[MarkerSite]]:
    sites = scan_markers(data)
    blobs = [
        Blob(
            name=s.name or f"<unnamed@{s.offset:#x}>",
            data=s.detail["raw"],  # type: ignore[index]
            origin="block",
            offset=s.offset,
            meta={"type_id": s.detail.get("type_id")},
        )
        for s in sites
        if s.kind == "block"
    ]
    return blobs, sites


@dataclass
class Container:
    path: Path
    kind: str
    blobs: list[Blob]
    markers: list[MarkerSite] = field(default_factory=list)
    file_size: int = 0

    def get(self, name: str) -> Blob | None:
        for blob in self.blobs:
            if blob.name == name:
                return blob
        return None


def open_container(
    path: str | Path, wanted: Callable[[str], bool] | None = None
) -> Container:
    """Open a part file and return its named blobs."""
    path = Path(path)
    with path.open("rb") as handle:
        head = handle.read(len(OLE2_MAGIC))
    if head.startswith(OLE2_MAGIC):
        return Container(
            path, "cfb", read_cfb(path, wanted), file_size=path.stat().st_size
        )

    data = path.read_bytes()
    kind = detect_container(data)
    if kind == "block":
        blobs, markers = read_block_container(data)
        return Container(path, kind, blobs, markers, file_size=len(data))
    raise ContainerError(
        f"{path.name}: neither OLE2 magic nor a block marker found "
        f"(first bytes: {data[:16].hex(' ')})"
    )

