"""The Parasolid transmit embedded in a part file."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from .carve import carve
from .container import Container

BINARY_MAGIC = b"PS"

TEXT_PREAMBLE = (
    b"**ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    b"**************************"
)
END_OF_HEADER = b"**END_OF_HEADER"

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")

_MIN_NAME = 3


class TransmitError(Exception):
    pass


@dataclass
class Transmit:
    form: str
    description: str
    schema: str
    body_offset: int
    data: bytes = field(repr=False)

    @property
    def guise(self) -> str:
        match = re.search(r"TRANSMIT FILE(?: \((\w+)\))?", self.description)
        if match is None:
            return "unknown"
        return match.group(1) or "transmit"

    @property
    def modeller_version(self) -> str | None:
        match = re.search(r"modeller version (\d+)", self.description)
        return match.group(1) if match else None

    @property
    def schema_key(self) -> str:
        return self.schema.rsplit("_", 1)[0]

    @property
    def base_schema(self) -> str | None:
        parts = self.schema.rsplit("_", 1)
        return parts[1] if len(parts) == 2 else None


def read_binary_transmit(data: bytes) -> Transmit:
    if not data.startswith(BINARY_MAGIC):
        raise TransmitError(f"no PS magic (starts {data[:8].hex(' ')})")
    if len(data) < 10:
        raise TransmitError("too short to hold a header")

    desc_len = struct.unpack_from(">I", data, 2)[0]
    if desc_len > len(data):
        raise TransmitError(f"description length {desc_len} exceeds the blob")
    at = 6 + desc_len
    description = data[6:at].decode("latin-1")

    if at + 4 > len(data):
        raise TransmitError("truncated before the schema length")
    schema_len = struct.unpack_from(">I", data, at)[0]
    at += 4
    if at + schema_len > len(data):
        raise TransmitError(f"schema length {schema_len} exceeds the blob")
    schema = data[at : at + schema_len].decode("latin-1")

    return Transmit(
        form="binary",
        description=description,
        schema=schema,
        body_offset=at + schema_len,
        data=data,
    )


def read_text_transmit(data: bytes) -> Transmit:
    if not data.startswith(TEXT_PREAMBLE[:20]):
        raise TransmitError("no Parasolid text preamble")
    marker = data.find(END_OF_HEADER)
    if marker == -1:
        raise TransmitError("no **END_OF_HEADER marker")

    line_end = data.find(b"\n", marker)
    body = unwrap(data[line_end + 1 :])

    match = re.match(rb"T(\d+) ", body)
    if match is None:
        raise TransmitError("body does not open with a T length record")
    desc_len = int(match.group(1))
    at = match.end()
    description = body[at : at + desc_len].decode("latin-1")
    at += desc_len

    match = re.match(rb"(\d+) ", body[at:])
    if match is None:
        raise TransmitError("no schema length after the description")
    schema_len = int(match.group(1))
    at += match.end()
    schema = body[at : at + schema_len].decode("latin-1")

    return Transmit(
        form="text",
        description=description,
        schema=schema,
        body_offset=at + schema_len,
        data=body,
    )


def unwrap(data: bytes) -> bytes:
    return data.replace(b"\r", b"").replace(b"\n", b"")


def read_transmit(data: bytes) -> Transmit:
    if data.startswith(BINARY_MAGIC):
        return read_binary_transmit(data)
    return read_text_transmit(data)


FIELD_NAME_SCAN_LIMIT = 1 << 20


def field_names(transmit: Transmit, *, limit: int = FIELD_NAME_SCAN_LIMIT) -> list[str]:
    window = transmit.data[:limit]
    if transmit.form == "binary":
        found = []
        for match in re.finditer(rb"[a-z][a-z0-9_]*", window):
            name = match.group().decode()
            start = match.start()
            if len(name) >= _MIN_NAME and start > 0 and window[start - 1] == len(name):
                found.append(name)
        return found

    text = window.decode("latin-1")
    found = []
    for match in re.finditer(r"(\d+) ", text):
        digits = match.group(1)
        for cut in range(len(digits)):
            suffix = digits[cut:]
            if suffix.startswith("0"):
                continue
            length = int(suffix)
            candidate = text[match.end() : match.end() + length]
            if length >= _MIN_NAME and _IDENTIFIER.match(candidate):
                found.append(candidate)
                break
    return found


_PARTITION_STREAM = re.compile(r"Config-(\d+)-Partition$")


def is_partition_stream(name: str) -> bool:
    return "Partition" in name


def main_partition(container: Container) -> Transmit | None:
    for stream, transmits in extract_transmits(container).items():
        if not _PARTITION_STREAM.search(stream):
            continue
        for transmit in transmits:
            if transmit.guise == "partition":
                return transmit
    return None


def extract_transmits(container: Container) -> dict[str, list[Transmit]]:
    out: dict[str, list[Transmit]] = {}
    for blob in container.blobs:
        if not is_partition_stream(blob.name):
            continue
        found = []
        for region in carve(blob.data, scan_bare=False):
            if region.data.startswith(BINARY_MAGIC):
                try:
                    found.append(read_binary_transmit(region.data))
                except TransmitError:
                    continue
        if found:
            out[blob.name] = found
    return out


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _transmit_date() -> str:
    import time

    t = time.localtime()
    return (
        f"{_DAYS[t.tm_wday]} {_MONTHS[t.tm_mon - 1]} {t.tm_mday:02d} "
        f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} {t.tm_year}"
    )


def to_part_transmit(
    transmit: Transmit,
    nodes,
    base,
    layouts,
    current,
    *,
    key: str = "part",
    max_node_types: int | None = None,
) -> str:
    from .xt.write import verify, write_transmit

    if not max_node_types:
        raise TransmitError("the source did not declare a node-type maximum")
    declared = max_node_types

    text = write_transmit(
        nodes,
        base,
        layouts,
        max_node_types=declared,
        date=_transmit_date(),
        modeller_version=transmit.modeller_version or "0",
        schema_key=transmit.schema_key,
        key=key,
        current=current,
    )
    verify(text, nodes, base, current, layouts=layouts)
    return text

