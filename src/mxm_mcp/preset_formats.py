"""Decoders for third-party preset files into native VST3 component state.

MXM's native VST3 host embeds a plugin's own opaque state in
``<vst3instrument><state encoding="base64">``. Many vendors ship patches in
their own containers instead: Native Instruments NKS banks, Xfer Serum 2
containers, Yamaha DX7 cartridges, JUCE XML/ValueTree files, Cherry Audio
ValueTree presets, Sonic Charge text patches and plain VST2 ``.fxp``/``.fxb``
chunks.

This module converts those files to the bytes the target plugin's
``setState``/``setComponentState`` expects. Decoders are pure functions over
bytes, so they are testable without an installed plugin. Formats that can only
be expressed as a modification of the plugin's current state accept a
``current_state`` template; without one they raise :class:`PresetFormatError`
instead of producing a state the plugin would reject.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import struct
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "PresetFormatError",
    "DecodedPreset",
    "SUPPORTED_EXTENSIONS",
    "BANK_EXTENSIONS",
    "detect_format",
    "describe_preset",
    "decode_preset",
    "split_bank_reference",
    "parse_nks",
    "nks_plugin_chunk",
    "is_xfer_json",
    "read_xfer",
    "write_xfer",
    "serum_preset_to_states",
    "is_serum_fx_file",
    "serum_fx_with_file",
    "read_value_tree",
    "write_value_tree",
    "value_tree_to_xml",
    "value_tree_to_juce_xml",
    "juce_xml_blob",
    "juce_xml_state",
    "juce_string_state",
    "is_cherry_preset",
    "cherry_with_preset",
    "is_dx7_cartridge",
    "dx7_voice_names",
    "unpack_dx7_voice",
    "dexed_with_voice",
    "is_synplant_patch",
    "is_echobode_patch",
    "is_permut8_bank",
    "synplant_with_patch",
    "echobode_with_patch",
    "permut8_bank_programs",
    "permut8_with_bank",
    "is_vst2_chunk",
    "fxp_chunk",
    "is_full_bucket_bank",
    "full_bucket_bank_programs",
    "obxd_bank_programs",
    "obxd_bank_state",
    "soundbox_state",
    "decent_sampler_state",
    "encode_juce_base64",
    "decode_juce_base64",
]


class PresetFormatError(ValueError):
    """A preset file cannot be converted to usable component state."""


# Extensions this module understands. ``.fxb``/``.fxp`` are VST2 chunks;
# ``.syx``/``.p8bank`` are expanded by :mod:`mxm_mcp.preset_catalog`.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".vstpreset", ".h2p", ".uhe-preset", ".vital",
    ".nksf", ".nksfx",
    ".serumpreset", ".serumfx", ".serumfxrack",
    ".syx", ".fxp", ".fxb",
    ".odin", ".dspreset", ".sbset", ".vvp",
    ".dco106preset", ".mg1preset", ".sempreset", ".voltagepreset",
    ".synplant", ".echobode", ".p8bank",
})

BANK_EXTENSIONS: frozenset[str] = frozenset({".syx", ".fxb", ".p8bank"})

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_NESTING = 64
_MAX_ITEMS = 1_000_000


# ---------------------------------------------------------------------------
# Small binary helpers
# ---------------------------------------------------------------------------

def _le32(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


def _le64(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def _be32(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset:offset + 4], "big")


def _put32(out: bytearray, value: int) -> None:
    out.extend(int(value & 0xFFFFFFFF).to_bytes(4, "little"))


def _put64(out: bytearray, value: int) -> None:
    out.extend(int(value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little"))


def _put_be32(out: bytearray, value: int) -> None:
    out.extend(int(value & 0xFFFFFFFF).to_bytes(4, "big"))


def _put_be32_at(out: bytearray, offset: int, value: int) -> None:
    out[offset:offset + 4] = int(value & 0xFFFFFFFF).to_bytes(4, "big")


def _decode_text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _strip_nul(raw: bytes) -> bytes:
    return raw.rstrip(b"\x00")


# ---------------------------------------------------------------------------
# zstd (Serum 2 payloads). Prefer the installed libzstd through ctypes and
# fall back to the zstd executable; neither is a hard import dependency.
# ---------------------------------------------------------------------------

_ZSTD_LIB: Any = None
_ZSTD_LOOKED_UP = False


def _zstd_library() -> Any:
    global _ZSTD_LIB, _ZSTD_LOOKED_UP
    if _ZSTD_LOOKED_UP:
        return _ZSTD_LIB
    _ZSTD_LOOKED_UP = True
    name = ctypes.util.find_library("zstd")
    if not name:
        return None
    try:
        lib = ctypes.CDLL(name)
        lib.ZSTD_decompress.restype = ctypes.c_size_t
        lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_compress.restype = ctypes.c_size_t
        lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_int]
        lib.ZSTD_compressBound.restype = ctypes.c_size_t
        lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_getErrorName.restype = ctypes.c_char_p
        lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
    except (OSError, AttributeError):
        return None
    _ZSTD_LIB = lib
    return lib


def _zstd_decompress(data: bytes, expected_size: int | None = None) -> bytes:
    lib = _zstd_library()
    if lib is not None:
        capacity = expected_size if expected_size and expected_size > 0 else max(
            len(data) * 16, 1 << 20)
        for _ in range(4):
            buffer = ctypes.create_string_buffer(capacity)
            source = ctypes.create_string_buffer(data, len(data))
            written = lib.ZSTD_decompress(buffer, capacity, source, len(data))
            if lib.ZSTD_isError(written):
                raise PresetFormatError(
                    f"zstd decompression failed: {lib.ZSTD_getErrorName(written).decode('ascii', 'replace')}")
            if expected_size is None or written == expected_size:
                return buffer.raw[:written]
            capacity = max(int(written), capacity * 2)
    try:
        completed = subprocess.run(
            ["zstd", "-d", "-q", "-c"], input=data, capture_output=True, check=False)
    except OSError as exc:
        raise PresetFormatError("zstd is unavailable for this preset") from exc
    if completed.returncode != 0:
        raise PresetFormatError("zstd payload does not decompress")
    if expected_size is not None and len(completed.stdout) != expected_size:
        raise PresetFormatError("zstd payload has an unexpected size")
    return completed.stdout


def _zstd_compress(data: bytes, level: int = 3) -> bytes:
    lib = _zstd_library()
    if lib is not None:
        capacity = lib.ZSTD_compressBound(len(data))
        buffer = ctypes.create_string_buffer(capacity)
        source = ctypes.create_string_buffer(data, len(data))
        written = lib.ZSTD_compress(buffer, capacity, source, len(data), level)
        if lib.ZSTD_isError(written):
            raise PresetFormatError("zstd compression failed")
        return buffer.raw[:written]
    try:
        completed = subprocess.run(
            ["zstd", f"-{max(1, level)}", "-q", "-c"], input=data,
            capture_output=True, check=False)
    except OSError as exc:
        raise PresetFormatError("zstd is unavailable for this preset") from exc
    if completed.returncode != 0:
        raise PresetFormatError("zstd compression failed")
    return completed.stdout


# ---------------------------------------------------------------------------
# msgpack (NKS NISI/PLID chunks)
# ---------------------------------------------------------------------------

def _msgpack_decode(data: bytes) -> Any:
    def read(pos: int, depth: int = 0) -> tuple[Any, int]:
        if depth > _MAX_NESTING:
            raise PresetFormatError("NKS metadata nested too deeply")
        if pos >= len(data):
            raise PresetFormatError("truncated NKS metadata")
        code = data[pos]
        pos += 1
        if code <= 0x7F:
            return code, pos
        if code >= 0xE0:
            return code - 256, pos
        if 0x80 <= code <= 0x8F:
            return _map(pos, code & 0x0F, depth)
        if 0x90 <= code <= 0x9F:
            return _array(pos, code & 0x0F, depth)
        if 0xA0 <= code <= 0xBF:
            length = code & 0x1F
            return _bytes_text(pos, length), pos + length

        def need(count: int) -> int:
            if pos + count > len(data):
                raise PresetFormatError("truncated NKS metadata")
            return pos

        if code == 0xC0:
            return None, pos
        if code == 0xC2:
            return False, pos
        if code == 0xC3:
            return True, pos
        if code in (0xC4, 0xC5, 0xC6):
            width = {0xC4: 1, 0xC5: 2, 0xC6: 4}[code]
            need(width)
            length = int.from_bytes(data[pos:pos + width], "big")
            pos += width
            need(length)
            return data[pos:pos + length], pos + length
        if code in (0xD9, 0xDA, 0xDB):
            width = {0xD9: 1, 0xDA: 2, 0xDB: 4}[code]
            need(width)
            length = int.from_bytes(data[pos:pos + width], "big")
            pos += width
            need(length)
            return _bytes_text(pos, length), pos + length
        if code in (0xCC, 0xCD, 0xCE, 0xCF):
            width = {0xCC: 1, 0xCD: 2, 0xCE: 4, 0xCF: 8}[code]
            need(width)
            value = int.from_bytes(data[pos:pos + width], "big")
            return value, pos + width
        if code in (0xD0, 0xD1, 0xD2, 0xD3):
            width = {0xD0: 1, 0xD1: 2, 0xD2: 4, 0xD3: 8}[code]
            need(width)
            value = int.from_bytes(data[pos:pos + width], "big", signed=True)
            return value, pos + width
        if code == 0xCA:
            need(4)
            return struct.unpack(">f", data[pos:pos + 4])[0], pos + 4
        if code == 0xCB:
            need(8)
            return struct.unpack(">d", data[pos:pos + 8])[0], pos + 8
        if code in (0xDC, 0xDD):
            width = 2 if code == 0xDC else 4
            need(width)
            count = int.from_bytes(data[pos:pos + width], "big")
            return _array(pos + width, count, depth)
        if code in (0xDE, 0xDF):
            width = 2 if code == 0xDE else 4
            need(width)
            count = int.from_bytes(data[pos:pos + width], "big")
            return _map(pos + width, count, depth)
        if code in (0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xC7, 0xC8, 0xC9):
            # Extension values: skip the payload, they carry no tag data NKS needs.
            if code == 0xD4:
                width = 1
            elif code == 0xD5:
                width = 2
            elif code == 0xD6:
                width = 4
            elif code == 0xD7:
                width = 8
            elif code == 0xD8:
                width = 16
            else:
                width_bytes = {0xC7: 1, 0xC8: 2, 0xC9: 4}[code]
                need(width_bytes)
                width = int.from_bytes(data[pos:pos + width_bytes], "big")
                pos += width_bytes
                need(1 + width)
                return None, pos + 1 + width
            need(1 + width)
            return None, pos + 1 + width
        raise PresetFormatError(f"unsupported NKS msgpack code 0x{code:02x}")

    def _bytes_text(pos: int, length: int) -> str:
        return _decode_text(data[pos:pos + length])

    def _array(pos: int, count: int, depth: int) -> tuple[list[Any], int]:
        if count > _MAX_ITEMS:
            raise PresetFormatError("NKS metadata array is too large")
        values = []
        for _ in range(count):
            value, pos = read(pos, depth + 1)
            values.append(value)
        return values, pos

    def _map(pos: int, count: int, depth: int) -> tuple[dict[Any, Any], int]:
        if count > _MAX_ITEMS:
            raise PresetFormatError("NKS metadata map is too large")
        values: dict[Any, Any] = {}
        for _ in range(count):
            key, pos = read(pos, depth + 1)
            value, pos = read(pos, depth + 1)
            values[key] = value
        return values, pos

    value, _ = read(0)
    return value


# ---------------------------------------------------------------------------
# CBOR (Serum 2 payloads)
# ---------------------------------------------------------------------------

def _cbor_decode(data: bytes) -> Any:
    def argument(pos: int, additional: int) -> tuple[int, int, bool]:
        if additional < 24:
            return additional, pos, True
        widths = {24: 1, 25: 2, 26: 4, 27: 8}
        if additional in widths:
            width = widths[additional]
            if pos + width > len(data):
                raise PresetFormatError("truncated Serum CBOR payload")
            return int.from_bytes(data[pos:pos + width], "big"), pos + width, True
        if additional == 31:
            return 0, pos, False
        raise PresetFormatError("invalid Serum CBOR header")

    def read(pos: int, depth: int = 0) -> tuple[Any, int]:
        if depth > _MAX_NESTING:
            raise PresetFormatError("Serum payload nested too deeply")
        if pos >= len(data):
            raise PresetFormatError("truncated Serum CBOR payload")
        head = data[pos]
        pos += 1
        major, additional = head >> 5, head & 0x1F
        value, pos, definite = argument(pos, additional)
        if major == 0:
            return value, pos
        if major == 1:
            return -1 - value, pos
        if major == 2:
            if not definite:
                chunks = bytearray()
                while pos < len(data) and data[pos] != 0xFF:
                    item, pos = read(pos, depth + 1)
                    if not isinstance(item, (bytes, bytearray)):
                        raise PresetFormatError("invalid Serum CBOR byte string")
                    chunks.extend(item)
                return bytes(chunks), pos + 1
            if pos + value > len(data):
                raise PresetFormatError("truncated Serum CBOR payload")
            return data[pos:pos + value], pos + value
        if major == 3:
            if not definite:
                text = ""
                while pos < len(data) and data[pos] != 0xFF:
                    item, pos = read(pos, depth + 1)
                    if not isinstance(item, str):
                        raise PresetFormatError("invalid Serum CBOR text string")
                    text += item
                return text, pos + 1
            if pos + value > len(data):
                raise PresetFormatError("truncated Serum CBOR payload")
            return _decode_text(data[pos:pos + value]), pos + value
        if major == 4:
            if definite and value > _MAX_ITEMS:
                raise PresetFormatError("Serum payload array is too large")
            values = []
            while (not definite and data[pos] != 0xFF) if pos < len(data) else False:
                item, pos = read(pos, depth + 1)
                values.append(item)
            if not definite:
                pos += 1
            else:
                for _ in range(value):
                    item, pos = read(pos, depth + 1)
                    values.append(item)
            return values, pos
        if major == 5:
            if definite and value > _MAX_ITEMS:
                raise PresetFormatError("Serum payload map is too large")
            values: dict[Any, Any] = {}
            if not definite:
                while pos < len(data) and data[pos] != 0xFF:
                    key, pos = read(pos, depth + 1)
                    item, pos = read(pos, depth + 1)
                    values[key] = item
                return values, pos + 1
            for _ in range(value):
                key, pos = read(pos, depth + 1)
                item, pos = read(pos, depth + 1)
                values[key] = item
            return values, pos
        if major == 6:
            item, pos = read(pos, depth + 1)
            return item, pos
        if major == 7:
            if additional == 20:
                return False, pos
            if additional == 21:
                return True, pos
            if additional in (22, 23):
                return None, pos
            if additional == 25:
                return struct.unpack(">e", data[pos - 2:pos])[0], pos
            if additional == 26:
                return struct.unpack(">f", data[pos - 4:pos])[0], pos
            if additional == 27:
                return struct.unpack(">d", data[pos - 8:pos])[0], pos
        raise PresetFormatError("unsupported Serum CBOR value")

    value, _ = read(0)
    return value


def _cbor_encode_head(major: int, value: int) -> bytes:
    prefix = major << 5
    if value < 24:
        return bytes([prefix | value])
    if value < 0x100:
        return bytes([prefix | 24, value])
    if value < 0x10000:
        return bytes([prefix | 25]) + value.to_bytes(2, "big")
    if value < 0x100000000:
        return bytes([prefix | 26]) + value.to_bytes(4, "big")
    return bytes([prefix | 27]) + value.to_bytes(8, "big")


def _cbor_encode(value: Any) -> bytes:
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _cbor_encode_head(0, value) if value >= 0 else _cbor_encode_head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _cbor_encode_head(3, len(raw)) + raw
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return _cbor_encode_head(2, len(raw)) + raw
    if isinstance(value, (list, tuple)):
        return _cbor_encode_head(4, len(value)) + b"".join(_cbor_encode(item) for item in value)
    if isinstance(value, dict):
        out = bytearray(_cbor_encode_head(5, len(value)))
        for key, item in value.items():
            out.extend(_cbor_encode(key))
            out.extend(_cbor_encode(item))
        return bytes(out)
    raise PresetFormatError(f"cannot encode {type(value).__name__} as CBOR")


# ---------------------------------------------------------------------------
# NKS (Native Instruments RIFF "NIKS")
# ---------------------------------------------------------------------------

@dataclass
class NksPreset:
    """Fields an NKS container exposes to preset discovery."""

    state: bytes | None = None
    name: str = ""
    vendor: str = ""
    bank: str = ""
    category: str = ""
    magic: str = ""
    uid: str = ""
    version: int = 0
    bankchain: list[str] = field(default_factory=list)


def _riff_chunks(data: bytes) -> Iterable[tuple[str, bytes]]:
    """Yield ``(id, payload)`` from a little-endian RIFF ``NIKS`` file."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"NIKS":
        raise PresetFormatError("not a Native Instruments NKS file")
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4].decode("ascii", "replace")
        size = _le32(data, offset + 4)
        start = offset + 8
        end = start + size
        if end > len(data):
            raise PresetFormatError(f"truncated NKS chunk {chunk_id}")
        yield chunk_id, data[start:end]
        offset = end + (size & 1)


def nks_plugin_chunk(data: bytes) -> bytes:
    """Return the plugin state inside an NKS container's PCHK chunk."""
    for chunk_id, payload in _riff_chunks(data):
        if chunk_id != "PCHK":
            continue
        if len(payload) < 4:
            raise PresetFormatError("PCHK chunk is too small")
        # Most NKS files prefix the state with a u32 version. A few wrapped
        # VST2 plugins store the state alone; keep recognised signatures intact.
        if payload.startswith((b"VST3", b"VC2!", b"CcnK", b"<", b"{")):
            return payload
        return payload[4:]
    raise PresetFormatError("NKS file has no PCHK plugin chunk")


def parse_nks(data: bytes) -> NksPreset:
    """Parse RIFF ``NIKS`` metadata and the PCHK component state."""
    result = NksPreset()
    try:
        result.state = nks_plugin_chunk(data)
    except PresetFormatError:
        result.state = None
    for chunk_id, payload in _riff_chunks(data):
        if chunk_id not in {"NISI", "PLID"} or len(payload) <= 4:
            continue
        # Metadata chunks also begin with a u32 version/offset.
        body = payload[4:]
        try:
            meta = _msgpack_decode(body)
        except PresetFormatError:
            continue
        if not isinstance(meta, dict):
            continue
        if chunk_id == "NISI":
            result.name = str(meta.get("name") or "")
            result.vendor = str(meta.get("vendor") or "")
            bankchain = meta.get("bankchain")
            if isinstance(bankchain, list) and bankchain:
                result.bankchain = [str(item) for item in bankchain if isinstance(item, str)]
                result.bank = result.bankchain[0] if result.bankchain else ""
                if len(result.bankchain) > 1:
                    result.category = result.bankchain[1]
            types = meta.get("types")
            if isinstance(types, list) and types and isinstance(types[0], list):
                text = "/".join(str(item) for item in types[0] if isinstance(item, str))
                if text:
                    result.category = text
        else:
            magic = meta.get("VST.magic")
            if isinstance(magic, int) and not isinstance(magic, bool):
                result.magic = f"{magic & 0xFFFFFFFF:08X}"
            uid = meta.get("VST3.uid")
            if isinstance(uid, list):
                parts = []
                for item in uid:
                    if isinstance(item, int) and not isinstance(item, bool):
                        parts.append(f"{item & 0xFFFFFFFF:08X}")
                result.uid = "".join(parts)
        # SynthMaster identifies its bank in the PCHK payload instead of PLID.
        if chunk_id == "PCHK" and len(payload) >= 28:
            if payload[8:12] == b"133k":
                if payload[12:16] == b"p1ms":
                    result.magic = "536D3169"
                elif payload[12:16] == b"lpms":
                    result.magic = "536D7069"
    return result


# ---------------------------------------------------------------------------
# Xfer Serum 2
# ---------------------------------------------------------------------------

def is_xfer_json(data: bytes) -> bool:
    return data[:9] == b"XferJson\x00"


def _xfer_header(data: bytes) -> tuple[bytes, dict[str, Any]]:
    if not is_xfer_json(data) or len(data) < 17:
        raise PresetFormatError("not an Xfer (Serum 2) preset")
    header_size = _le64(data, 9)
    if 17 + header_size > len(data):
        raise PresetFormatError("truncated Xfer header")
    raw = _strip_nul(data[17:17 + header_size])
    try:
        header = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeError) as exc:
        raise PresetFormatError("Xfer header is not JSON") from exc
    if not isinstance(header, dict):
        raise PresetFormatError("Xfer header is not a JSON object")
    return data[17 + header_size:], header


def _xfer_payload(data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    rest, header = _xfer_header(data)
    if len(rest) < 8:
        raise PresetFormatError("truncated Xfer payload")
    size = _le32(rest, 0)
    version = _le32(rest, 4)
    if size > (256 << 20):
        raise PresetFormatError("Xfer payload is larger than any preset")
    compressed = rest[8:]
    if version == 2:
        raw = _zstd_decompress(compressed, size)
        payload = _cbor_decode(raw)
    elif version == 0:
        raw = zlib.decompress(compressed)
        payload = _ubjson_decode(raw)
    else:
        raise PresetFormatError(f"unsupported Xfer payload version {version}")
    if not isinstance(payload, dict):
        raise PresetFormatError("Xfer payload is not a map")
    return header, payload


def read_xfer(data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the JSON header and the CBOR/UBJSON payload of a Serum 2 file."""
    return _xfer_payload(data)


def write_xfer(header: dict[str, Any], payload: dict[str, Any]) -> bytes:
    """Build an Xfer container, matching Serum's own serialization."""
    import hashlib

    raw = _cbor_encode(payload)
    compressed = _zstd_compress(raw, 3)
    header = dict(header)
    header["hash"] = hashlib.md5(compressed).hexdigest()
    text = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    out = bytearray(b"XferJson\x00")
    _put64(out, len(text))
    out.extend(text)
    _put32(out, len(raw))
    _put32(out, 2)
    out.extend(compressed)
    return bytes(out)


def serum_preset_to_states(data: bytes) -> tuple[bytes | None, bytes | None]:
    """Split a Serum 2 preset into processor and controller component states."""
    if is_xfer_json(data):
        try:
            rest, header = _xfer_header(data)
            if header.get("component") == "processor":
                return data, None
        except Exception:
            pass
    header, payload = _xfer_payload(data)
    if header.get("component") == "processor":
        return data, None
    controller_only = {
        "ClipPlayer", "Filter", "GranularOsc", "MultiSampleOsc", "Osc",
        "SerumGUI", "SpectralOsc", "WTOsc", "arpBankDisplayName",
        "clipBankDisplayName", "presetAuthor", "presetDescription", "presetName",
    }

    def shared(key: str) -> bool:
        return any(key.startswith(prefix) for prefix in
                   ("ArpClip", "MidiClip", "FXRack", "VoicePanel", "LFO")) \
            and "PointModBus" not in key

    processor: dict[str, Any] = {}
    controller: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "fileType":
            continue
        if key not in controller_only:
            processor[key] = value
        if key in controller_only or shared(key):
            controller[key] = value
    processor["component"] = "processor"
    controller["component"] = "controller"
    controller["presetHasBeenEdited"] = False
    metadata = {key: header[key] for key in
                ("product", "productVersion", "url", "vendor", "version")
                if key in header}
    processor.update(metadata)
    controller.update(metadata)
    processor_header = dict(metadata, component="processor")
    controller_header = dict(metadata, component="controller")
    for key in ("presetName", "presetAuthor", "presetDescription"):
        controller_header[key] = header.get(key, "")
    return write_xfer(processor_header, processor), write_xfer(controller_header, controller)


def is_serum_fx_file(data: bytes) -> bool:
    if not is_xfer_json(data) or len(data) < 17:
        return False
    header_size = _le64(data, 9)
    if 17 + header_size > len(data):
        return False
    try:
        header = json.loads(_decode_text(_strip_nul(data[17:17 + header_size])))
    except ValueError:
        return False
    return isinstance(header, dict) and header.get("fileType") in {"SerumFX", "SerumFXRack"}


def serum_fx_with_file(current_state: bytes, file_data: bytes) -> bytes:
    """Replace the rack modules of Serum 2 FX's state with a preset file's."""
    _file_header, file_payload = _xfer_payload(file_data)
    current_header, current_payload = _xfer_payload(current_state)
    if current_header.get("component") != "processor" or "FXRack0" not in current_payload:
        raise PresetFormatError("a Serum FX file loads into Serum 2 FX only")
    file_type = current_header.get("fileType", file_payload.get("fileType", ""))
    if file_type == "SerumFXRack":
        rack = file_payload.get("FXRack")
        if not isinstance(rack, dict):
            raise PresetFormatError("the Serum rack file has no FXRack")
    elif file_type == "SerumFX":
        skip = {"fileType", "fxType", "product", "productVersion", "url",
                "vendor", "version", "hash"}
        rack = {"FX": [{key: value for key, value in file_payload.items() if key not in skip}]}
    else:
        raise PresetFormatError("is not a Serum FX or FX rack file")
    target = current_payload.get("FXRack0")
    if not isinstance(target, dict):
        target = {}
        current_payload["FXRack0"] = target
    target["FX"] = rack.get("FX", [])
    if "plainParams" in rack and rack["plainParams"] != "default":
        target["plainParams"] = rack["plainParams"]
    if "parameters" in rack:
        target["parameters"] = rack["parameters"]
    current_header.pop("hash", None)
    return write_xfer(current_header, current_payload)


# ---------------------------------------------------------------------------
# UBJSON (older Serum 2 payloads)
# ---------------------------------------------------------------------------

def _ubjson_decode(data: bytes) -> Any:
    def read(pos: int, depth: int = 0) -> tuple[Any, int]:
        if depth > _MAX_NESTING or pos >= len(data):
            raise PresetFormatError("invalid UBJSON payload")
        marker = chr(data[pos])
        pos += 1
        if marker == "Z":
            return None, pos
        if marker == "N":
            return None, pos
        if marker == "T":
            return True, pos
        if marker == "F":
            return False, pos
        if marker in {"i", "U", "I", "l", "L"}:
            width = {"i": 1, "U": 1, "I": 2, "l": 4, "L": 8}[marker]
            if pos + width > len(data):
                raise PresetFormatError("truncated UBJSON payload")
            signed = marker in {"i", "l", "L"}
            return int.from_bytes(data[pos:pos + width], "big", signed=signed), pos + width
        if marker in {"d", "D"}:
            width = 4 if marker == "d" else 8
            if pos + width > len(data):
                raise PresetFormatError("truncated UBJSON payload")
            return struct.unpack(">f" if width == 4 else ">d",
                                 data[pos:pos + width])[0], pos + width
        if marker == "C":
            return chr(data[pos]), pos + 1
        if marker == "S":
            # Strings may carry an explicit length ('S' + count + string).
            if data[pos:pos + 1] in {b"i", b"U", b"I", b"l", b"L"}:
                count, pos = read(pos, depth + 1)
                if not isinstance(count, int) or count < 0 or pos + count > len(data):
                    raise PresetFormatError("invalid UBJSON string length")
                return _decode_text(data[pos:pos + count]), pos + count
            end = data.find(b"\x00", pos)
            if end < 0:
                end = len(data)
            return _decode_text(data[pos:end]), end + (end < len(data))
        if marker in "[{":
            close = "]" if marker == "[" else "}"
            if data[pos:pos + 1] in {b"i", b"U", b"I", b"l", b"L"}:
                count, pos = read(pos, depth + 1)
                values = []
                for _ in range(int(count)):
                    item, pos = read(pos, depth + 1)
                    values.append(item)
                return (values if marker == "[" else {}), pos
            values = []
            while pos < len(data) and chr(data[pos]) != close:
                item, pos = read(pos, depth + 1)
                values.append(item)
            pos += 1
            if marker == "[":
                return values, pos
            return {values[i]: values[i + 1] for i in range(0, len(values) - 1, 2)}, pos
        raise PresetFormatError(f"unsupported UBJSON marker {marker!r}")

    value, _ = read(0)
    return value


# ---------------------------------------------------------------------------
# JUCE ValueTree binary and JUCE XML state
# ---------------------------------------------------------------------------

class _Reader:
    __slots__ = ("data", "index", "ok")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.index = 0
        self.ok = True

    def byte(self) -> int:
        if self.index >= len(self.data):
            self.ok = False
            return 0
        value = self.data[self.index]
        self.index += 1
        return value

    def cint(self) -> int:
        head = self.byte()
        count = head & 0x7F
        if count > 8:
            self.ok = False
            return 0
        value = 0
        for shift in range(count):
            value |= self.byte() << (8 * shift)
        return -value if head & 0x80 else value

    def cstr(self) -> str:
        start = self.index
        end = self.data.find(b"\x00", start)
        if end < 0:
            self.ok = False
            self.index = len(self.data)
            return _decode_text(self.data[start:])
        self.index = end + 1
        return _decode_text(self.data[start:end])


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("\n", "&#10;")
            .replace("\r", "&#13;").replace("\t", "&#9;"))


def _read_var(reader: _Reader) -> str:
    size = reader.cint()
    if size <= 0:
        return ""
    start = reader.index
    end = start + size
    if end > len(reader.data):
        raise PresetFormatError("truncated ValueTree value")
    value_type = reader.byte()
    payload = reader.data[reader.index:end]
    need = 4 if value_type == 1 else 8 if value_type in {4, 6} else 0
    if len(payload) < need:
        raise PresetFormatError("truncated ValueTree value")
    if value_type == 1:
        text = str(int.from_bytes(payload[:4], "little", signed=True))
    elif value_type == 2:
        text = "1"
    elif value_type == 3:
        text = "0"
    elif value_type == 4:
        text = format(struct.unpack("<d", payload[:8])[0], ".17g")
    elif value_type == 5:
        text = _decode_text(payload).rstrip("\x00")
    elif value_type == 6:
        text = str(int.from_bytes(payload[:8], "little", signed=True))
    elif value_type == 9:
        text = ""
    else:
        raise PresetFormatError(
            f"ValueTree value type {value_type} (array or binary) is not supported")
    reader.index = end
    return text


def _read_tree_xml(reader: _Reader, depth: int = 0) -> str:
    name = reader.cstr()
    if not reader.ok or not name or depth > _MAX_NESTING:
        raise PresetFormatError("not a JUCE ValueTree")
    xml = "<" + name
    props = reader.cint()
    if props < 0 or props > 100_000:
        raise PresetFormatError("not a JUCE ValueTree")
    for _ in range(props):
        key = reader.cstr()
        xml += f' {key}="{_xml_escape(_read_var(reader))}"'
    kids = reader.cint()
    if not reader.ok or kids < 0 or kids > 100_000:
        raise PresetFormatError("not a JUCE ValueTree")
    if not kids:
        return xml + "/>"
    return xml + ">" + "".join(_read_tree_xml(reader, depth + 1) for _ in range(kids)) + f"</{name}>"


def value_tree_to_xml(data: bytes) -> str:
    """Convert JUCE ``ValueTree::writeToStream`` bytes to its XML document."""
    reader = _Reader(data)
    xml = _read_tree_xml(reader)
    return '<?xml version="1.0" encoding="UTF-8"?>\n\n' + xml + "\n"


def juce_xml_blob(xml: str) -> bytes:
    """Wrap an XML document as JUCE binary XML (``VC2!`` + length + XML + NUL)."""
    body = xml.encode("utf-8") + b"\x00"
    return b"VC2!" + len(body).to_bytes(4, "little") + body


def value_tree_to_juce_xml(data: bytes) -> bytes:
    return juce_xml_blob(value_tree_to_xml(data))


@dataclass
class ValueTreeNode:
    type: str
    props: list[tuple[str, bytes]] = field(default_factory=list)
    kids: list["ValueTreeNode"] = field(default_factory=list)

    def child(self, name: str) -> "ValueTreeNode | None":
        return next((kid for kid in self.kids if kid.type == name), None)

    def set_prop(self, name: str, value: bytes) -> None:
        for index, (key, _old) in enumerate(self.props):
            if key == name:
                self.props[index] = (name, value)
                return
        self.props.append((name, value))

    def set_props(self, source: "ValueTreeNode") -> None:
        for key, value in source.props:
            self.set_prop(key, value)


def _read_value_tree(reader: _Reader, depth: int = 0) -> ValueTreeNode:
    name = reader.cstr()
    if not reader.ok or not name or depth > _MAX_NESTING:
        raise PresetFormatError("not a JUCE ValueTree")
    props = reader.cint()
    if props < 0 or props > _MAX_ITEMS:
        raise PresetFormatError("not a JUCE ValueTree")
    node = ValueTreeNode(name)
    for _ in range(props):
        key = reader.cstr()
        size = reader.cint()
        if not reader.ok or size < 0 or reader.index + size > len(reader.data):
            raise PresetFormatError("truncated ValueTree property")
        node.props.append((key, reader.data[reader.index:reader.index + size]))
        reader.index += size
    kids = reader.cint()
    if not reader.ok or kids < 0 or kids > _MAX_ITEMS:
        raise PresetFormatError("not a JUCE ValueTree")
    node.kids = [_read_value_tree(reader, depth + 1) for _ in range(kids)]
    return node


def read_value_tree(data: bytes) -> ValueTreeNode:
    """Read a binary ValueTree, keeping property values byte-exact."""
    return _read_value_tree(_Reader(data))


def _write_cint(out: bytearray, value: int) -> None:
    if value == 0:
        out.append(0)
        return
    negative = value < 0
    raw = value if not negative else -value
    payload = bytearray()
    while raw:
        payload.append(raw & 0xFF)
        raw >>= 8
    out.append(len(payload) | (0x80 if negative else 0))
    out.extend(payload)


def write_value_tree(node: ValueTreeNode) -> bytes:
    """Serialize a :class:`ValueTreeNode` as binary ValueTree bytes."""
    out = bytearray()

    def emit(item: ValueTreeNode) -> None:
        out.extend(item.type.encode("utf-8") + b"\x00")
        _write_cint(out, len(item.props))
        for key, value in item.props:
            out.extend(key.encode("utf-8") + b"\x00")
            _write_cint(out, len(value))
            out.extend(value)
        _write_cint(out, len(item.kids))
        for kid in item.kids:
            emit(kid)

    emit(node)
    return bytes(out)


def juce_xml_state(xml: bytes | str, *, preset_path: str | Path | None = None) -> bytes:
    """Convert a plugin's XML preset into a JUCE binary XML component state."""
    text = xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml
    text = text.rstrip("\x00 \t\r\n")
    # Vaporizer2 reads patch files as display values; a V2.00000 state is read
    # as internal values, so a V2.10000 wrapper selects the same generation.
    legacy = 'PatchVersion="VASTVaporizerParamsV2.00000"'
    root = text.find("<VASTvaporizer2 ")
    version = text.find(legacy)
    if root >= 0 and version >= 0 and version < text.find(">", root):
        text = text[:version + 36] + "1" + text[version + 37:]
    if not text.lstrip().startswith("<?xml"):
        text = '<?xml version="1.0" encoding="UTF-8"?>\n' + text
    return juce_xml_blob(text)


def juce_string_state(data: bytes) -> bytes:
    """A JUCE plugin's plain string state (Vital ``.vital``), NUL terminated."""
    state = data if data.endswith(b"\x00") else data + b"\x00"
    return state


def decent_sampler_state(data: bytes, preset_path: str | Path) -> bytes:
    text = _decode_text(data)
    root = text.find("<DecentSampler")
    if root < 0:
        raise PresetFormatError("not a DecentSampler preset (.dspreset)")
    directory = str(Path(preset_path).parent)
    text = text[:root + 14] + f' _samplePath="{_xml_escape(directory)}"' + text[root + 14:]
    return juce_xml_blob(text if text.lstrip().startswith("<?xml")
                         else '<?xml version="1.0" encoding="UTF-8"?>\n' + text)


def soundbox_state(data: bytes) -> bytes:
    text = _decode_text(data)
    start = text.find("<Audiomodern.Soundbox_Preset")
    if start < 0:
        raise PresetFormatError("not a Soundbox preset (.sbset)")
    text = text[start:].replace(
        "<Audiomodern.Soundbox_Preset", '<Audiomodern.Soundbox_State preset=""', 1)
    text = text.replace("</Audiomodern.Soundbox_Preset>",
                        "</Audiomodern.Soundbox_State>")
    return juce_xml_blob('<?xml version="1.0" encoding="UTF-8"?>\n' + text)


# ---------------------------------------------------------------------------
# Cherry Audio (JUCE ValueTree "main" merged into the plugin's savedState)
# ---------------------------------------------------------------------------

def is_cherry_preset(data: bytes) -> bool:
    return len(data) > 6 and data[:5] == b"main\x00"


def _value_tree_string(text: str) -> bytes:
    return b"\x05" + text.encode("utf-8") + b"\x00"


def cherry_with_preset(current_state: bytes | None, preset_data: bytes) -> bytes:
    """Merge a Cherry Audio preset tree into a plugin's ``savedState`` tree."""
    try:
        preset = read_value_tree(preset_data)
    except (PresetFormatError, IndexError) as exc:
        raise PresetFormatError(
            'not a Cherry Audio preset (JUCE ValueTree "main")') from exc
    if preset.type != "main":
        raise PresetFormatError('not a Cherry Audio preset (JUCE ValueTree "main")')
    display_name = next((value for key, value in preset.props
                         if key == "presetName"), None)
    if current_state and len(current_state) > 0xB0 \
            and current_state[:4] == b"VstW" and current_state[16:20] == b"CcnK":
        # Voltage Modular: VstW header, FXB header, JUCE chunk from 0xB0.
        chunk = current_state[0xB0:]
        reader = _Reader(chunk)
        root = _read_value_tree(reader)
        preset_info = root.child("presetInfo")
        if preset_info is None:
            raise PresetFormatError("unexpected Voltage Modular state")
        tail = chunk[reader.index:]
        preset_info.set_props(preset)
        if display_name is not None:
            preset_info.set_prop("displayName", display_name)
        preset_info.kids = list(preset.kids)
        new_chunk = write_value_tree(root) + tail
        out = bytearray(current_state[:0xB0])
        _put_be32_at(out, 20, len(out) - 24 + len(new_chunk))
        _put_be32_at(out, 0xAC, len(new_chunk))
        out.extend(new_chunk)
        return bytes(out)

    if current_state:
        reader = _Reader(current_state)
        root = _read_value_tree(reader)
        target = root.child("pt")
        if target is None:
            raise PresetFormatError("a Cherry Audio preset loads into its own plugin only")
        tail = current_state[reader.index:]
        target.set_props(preset)
        for child in preset.kids:
            existing = target.child(child.type)
            if child.type == "pd" and existing is not None:
                existing.set_props(child)
            elif existing is not None:
                target.kids[target.kids.index(existing)] = child
            else:
                target.kids.append(child)
        current = root.child("curpreset")
        if current is not None and display_name is not None:
            current.set_prop("displayName", display_name)
            current.set_prop("name", display_name)
        return write_value_tree(root) + tail

    # No template: build the minimal savedState tree the preset's plugin reads.
    pt = ValueTreeNode("pt", list(preset.props), list(preset.kids))
    curpreset = ValueTreeNode("curpreset")
    if display_name is not None:
        curpreset.props = [("displayName", display_name), ("name", display_name)]
    return write_value_tree(ValueTreeNode(
        "savedState", [], [curpreset, pt]))


# ---------------------------------------------------------------------------
# DX7 cartridges for Dexed
# ---------------------------------------------------------------------------

_JUCE_B64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"


def encode_juce_base64(data: bytes) -> str:
    """JUCE ``MemoryBlock::toBase64Encoding`` (little-endian 6-bit groups)."""
    out = [str(len(data)), "."]
    bits = len(data) * 8
    for bit in range(0, bits, 6):
        value = 0
        for offset in range(6):
            index = bit + offset
            if index < bits and (data[index // 8] >> (index % 8)) & 1:
                value |= 1 << offset
        out.append(_JUCE_B64[value])
    return "".join(out)


def decode_juce_base64(text: str) -> bytes:
    dot = text.find(".")
    if dot < 0:
        return b""
    try:
        size = int(text[:dot])
    except ValueError:
        return b""
    out = bytearray(size)
    bit = 0
    for char in text[dot + 1:]:
        position = _JUCE_B64.find(char)
        value = position if position >= 0 else 0
        for offset in range(6):
            if bit // 8 >= size:
                return bytes(out)
            if (value >> offset) & 1:
                out[bit // 8] |= 1 << (bit % 8)
            bit += 1
    return bytes(out)


def is_dx7_cartridge(data: bytes) -> bool:
    return (len(data) == 4104 and data[0] == 0xF0 and data[1] == 0x43
            and data[3] == 0x09 and data[4] == 0x20 and data[5] == 0x00)


def dx7_voice_names(data: bytes) -> list[str]:
    if not is_dx7_cartridge(data):
        return []
    names = []
    for voice in range(32):
        raw = data[6 + voice * 128 + 118:6 + voice * 128 + 128]
        names.append(_decode_text(raw).rstrip(" \x00"))
    return names


def unpack_dx7_voice(record: bytes) -> bytes:
    """Expand a packed 128-byte DX7 bulk voice into the 155-byte VCED layout."""
    if len(record) < 128:
        raise PresetFormatError("truncated DX7 voice")
    out = bytearray()
    for operator in range(6):
        q = record[operator * 17:(operator + 1) * 17]
        out.extend(q[0:11])
        out.append(q[11] & 3)
        out.append((q[11] >> 2) & 3)
        out.append(q[12] & 7)
        out.append(q[13] & 3)
        out.append((q[13] >> 2) & 7)
        out.append(q[14])
        out.append(q[15] & 1)
        out.append((q[15] >> 1) & 31)
        out.append(q[16])
        out.append((q[12] >> 3) & 15)
    out.extend(record[102:110])
    out.append(record[110] & 31)
    out.append(record[111] & 7)
    out.append((record[111] >> 3) & 1)
    out.extend(record[112:116])
    out.append(record[116] & 1)
    out.append((record[116] >> 1) & 7)
    out.append((record[116] >> 4) & 7)
    out.append(record[117])
    out.extend(record[118:128])
    return bytes(out)


def _set_xml_attribute(xml: str, name: str, value: str) -> str:
    key = f' {name}="'
    start = xml.find(key)
    if start < 0:
        return xml
    begin = start + len(key)
    end = xml.find('"', begin)
    if end < 0:
        return xml
    return xml[:begin] + value + xml[end:]


def dexed_with_voice(current_state: bytes | None, cartridge: bytes,
                     voice: int) -> bytes:
    """Patch one DX7 voice into Dexed's JUCE XML state."""
    if not is_dx7_cartridge(cartridge):
        raise PresetFormatError(
            "a DX7 cartridge must be a 4104-byte 32-voice bulk dump")
    if voice < 0 or voice > 31:
        raise PresetFormatError("DX7 voices are numbered 0-31")
    if current_state:
        if len(current_state) < 9 or current_state[:4] != b"VC2!":
            raise PresetFormatError(
                "a DX7 cartridge loads into Dexed only (its state is JUCE XML)")
        xml = _decode_text(current_state[8:]).rstrip("\x00")
        if "<dexedState" not in xml:
            raise PresetFormatError("a DX7 cartridge loads into Dexed only")
    else:
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n\n'
               '<dexedState currentProgram="0" opSwitch="111111" '
               'base64:sysex="" base64:program=""/>')
    program = bytearray(161)
    marker = ' base64:program="'
    start = xml.find(marker)
    if start >= 0:
        end = xml.find('"', start + len(marker))
        existing = decode_juce_base64(xml[start + len(marker):end])
        program[155:161] = existing[155:161]
        program.extend(b"\x00" * max(0, 161 - len(program)))
    program[:155] = unpack_dx7_voice(cartridge[6 + voice * 128:6 + (voice + 1) * 128])
    program[155] = 0x3F
    xml = _set_xml_attribute(xml, "base64:sysex", encode_juce_base64(cartridge))
    xml = _set_xml_attribute(xml, "base64:program", encode_juce_base64(bytes(program)))
    xml = _set_xml_attribute(xml, "currentProgram", str(voice))
    xml = _set_xml_attribute(xml, "opSwitch", "111111")
    return juce_xml_blob(xml)


# ---------------------------------------------------------------------------
# Sonic Charge (Synplant / Echobode / Permut8)
# ---------------------------------------------------------------------------

_SC_MAGIC = b"\x59\xa2\xcd\x18"


def is_synplant_patch(data: bytes) -> bool:
    return data[:16] == b"SynplantPatch: {"


def is_echobode_patch(data: bytes) -> bool:
    return data[:16] == b"EchobodePatch: {"


def is_permut8_bank(data: bytes) -> bool:
    return data[:16] == b"Permut8BankV1: {"


@dataclass
class _ScState:
    prefix: bytes = b""
    head: bytes = b""
    vst3: bool = False
    wrapped: bool = False
    chunk_header: bytes = b""
    body: bytes = b""


def _sc_unpack(state: bytes, fxid: str, plugin: str, what: str) -> _ScState:
    wrong = f"a {what} loads into {plugin} only"
    result = _ScState()
    offset = 0
    if len(state) >= 10 and state[:4] == _SC_MAGIC:
        result.vst3 = True
        result.prefix = state[:10]
        offset = 10
    chunk_at, chunk_size = offset, len(state) - offset
    if len(state) >= offset + 60 and state[offset:offset + 4] == b"CcnK":
        bank = state[offset + 8:offset + 12] == b"FBCh"
        program = state[offset + 8:offset + 12] == b"FPCh"
        if not bank and not program:
            raise PresetFormatError(wrong)
        if state[offset + 16:offset + 20] != fxid.encode("ascii"):
            raise PresetFormatError(wrong)
        size_at = offset + (156 if bank else 56)
        if len(state) < size_at + 4:
            raise PresetFormatError(f"unexpected {plugin} state layout")
        result.wrapped = True
        result.head = state[offset:size_at]
        chunk_size = _be32(state, size_at)
        chunk_at = size_at + 4
        if chunk_at + chunk_size > len(state):
            raise PresetFormatError(f"unexpected {plugin} state layout")
    elif result.vst3:
        raise PresetFormatError(wrong)
    chunk = state[chunk_at:chunk_at + chunk_size]
    if chunk_size < 8:
        raise PresetFormatError(f"unexpected {plugin} state layout")
    if chunk[4:8] == b"mrAM":
        header = 16
        reversed_id = fxid[::-1].encode("ascii")
        if chunk_size < 20 or chunk[8:12] != reversed_id:
            raise PresetFormatError(wrong)
    elif not result.wrapped and fxid != "NuPr":
        raise PresetFormatError(wrong)
    else:
        header = 4
    if chunk_size < header + 4:
        raise PresetFormatError(f"unexpected {plugin} state layout")
    body_size = _le32(chunk, header)
    try:
        body = zlib.decompress(chunk[header + 4:])
    except zlib.error as exc:
        raise PresetFormatError(f"{plugin} state does not decompress") from exc
    if len(body) != body_size:
        raise PresetFormatError(f"{plugin} state has an unexpected size")
    result.chunk_header = chunk[:header]
    result.body = body
    return result


def _sc_pack(sc: _ScState, body: bytes, current: int) -> bytes:
    compressed = zlib.compress(body, 6)
    new_chunk = bytearray(sc.chunk_header)
    _put32(new_chunk, len(body))
    new_chunk.extend(compressed)
    if not sc.wrapped:
        return bytes(new_chunk)
    rest = bytearray(sc.head[8:])
    _put_be32(rest, len(new_chunk))
    rest.extend(new_chunk)
    fx = bytearray(b"CcnK")
    _put_be32(fx, len(rest))
    fx.extend(rest)
    if not sc.vst3:
        return bytes(fx)
    out = bytearray(_SC_MAGIC)
    out.extend(b"\x00" + bytes([current & 0xFF]))
    _put32(out, len(fx))
    out.extend(fx)
    return bytes(out)


def _sc_with_patch(state: bytes, patch: bytes, name: str, fxid: str,
                   plugin: str, what: str) -> bytes:
    sc = _sc_unpack(state, fxid, plugin, what)
    body = sc.body
    single = (len(body) >= 8 and _le32(body, 0) == len(body) - 4
              and 0x41 <= body[4] <= 0x7A)
    count = 0 if single else (_le32(body, 0) if len(body) >= 4 else 0)
    position = 4
    patches: list[bytes] = []
    for _ in range(count):
        if position + 4 > len(body):
            break
        size = _le32(body, position)
        if position + 4 + size > len(body):
            break
        patches.append(body[position + 4:position + 4 + size])
        position += 4 + size
    if not single and not patches:
        raise PresetFormatError(f"{plugin} state has no programs")
    trailer = b"" if single else body[position:]
    if single:
        patches.append(b"")
    text = _decode_text(patch).replace("\r", "")
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    marker = text.find("\n\tname: \"")
    if marker >= 0:
        end = text.find("\n", marker + 1)
        text = text[:marker + 1] + f'\tname: "{escaped}"' + text[end:]
    else:
        cursor = text.find("\n\t")
        while cursor >= 0:
            if cursor + 2 < len(text) and text[cursor + 2] == "\t":
                cursor = text.find("\n\t", cursor + 1)
                continue
            colon = text.find(":", cursor)
            if colon >= 0 and text[cursor + 2:colon] > "name":
                text = text[:cursor + 1] + f'\tname: "{escaped}"\n' + text[cursor + 1:]
                break
            cursor = text.find("\n\t", cursor + 1)
    if not text.endswith("\n"):
        text += "\n"
    patches[0] = text.encode("utf-8")
    new_body = bytearray()
    if not single:
        _put32(new_body, len(patches))
    for item in patches:
        _put32(new_body, len(item))
        new_body.extend(item)
    new_body.extend(trailer)
    return _sc_pack(sc, bytes(new_body), 0)


def synplant_with_patch(current_state: bytes, patch: bytes, name: str) -> bytes:
    return _sc_with_patch(current_state, patch, name, "NuSP", "Synplant",
                          ".synplant patch")


def echobode_with_patch(current_state: bytes, patch: bytes, name: str) -> bytes:
    return _sc_with_patch(current_state, patch, name, "NuEB", "Echobode",
                          ".echobode patch")


@dataclass
class _Permut8Program:
    slot: str = ""
    name: str = ""
    modified: bool = False
    fields: dict[str, str] = field(default_factory=dict)


def _parse_permut8_bank(data: bytes) -> tuple[str, list[_Permut8Program]]:
    text = _decode_text(data).replace("\r", "")
    if not text.startswith("Permut8BankV1: {"):
        raise PresetFormatError("not a Permut8 bank")
    current = ""
    programs: list[_Permut8Program] = []
    active: _Permut8Program | None = None
    for line in text.splitlines():
        if line.startswith("\tCurrent Program: "):
            current = line[18:].strip()
        elif (len(line) == 7 and line.startswith("\t\t")
              and line[2] in "ABC" and line[3].isdigit() and line[4:7] == ": {"):
            active = _Permut8Program(slot=line[2:4])
            programs.append(active)
        elif active is not None and line.startswith("\t\t}"):
            active = None
        elif active is not None and line.startswith("\t\t\t"):
            split = line.find(": ")
            if split < 0:
                continue
            key, value = line[:split].strip(), line[split + 2:].strip()
            if key == "Name":
                raw = value[1:-1] if len(value) >= 2 and value[0] == '"' and value[-1] == '"' else value
                unescaped = ""
                index = 0
                while index < len(raw):
                    if raw[index] == "\\" and index + 1 < len(raw):
                        index += 1
                    unescaped += raw[index]
                    index += 1
                active.name = unescaped
            elif key == "Modified":
                active.modified = value == "true"
            else:
                active.fields[key] = value
    if len(programs) != 30:
        raise PresetFormatError("not a Permut8 bank with 30 programs")
    return current, programs


def permut8_bank_programs(data: bytes) -> list[str]:
    _current, programs = _parse_permut8_bank(data)
    names = [""] * 30
    for program in programs:
        slot = (ord(program.slot[0]) - ord("A")) * 10 + int(program.slot[1])
        names[slot] = program.name or program.slot
    return names


def _permut8_slot(slot: str) -> int:
    return (ord(slot[0]) - ord("A")) * 10 + int(slot[1])


def _permut8_number(text: str) -> float:
    start = next((index for index, char in enumerate(text)
                  if char in "+-.0123456789"), len(text))
    try:
        return float(text[start:])
    except ValueError:
        return 0.0


def _permut8_normalize(key: str, text: str) -> float:
    import math

    enums = {
        "FilterPlacement": ["Off", "Input", "Feedback", "Output"],
        "SyncMode": ["Off", "Dotted", "Triplets", "Standard"],
        "Operator1": ["NOP", "AND", "MUL", "OSC", "RND"],
        "Operator2": ["NOP", "OR", "XOR", "MSK", "SUB"],
    }

    def clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    if key in {"Limiter", "FeedbackFlip", "FeedbackInvert", "Reverse"}:
        return 1.0 if text == "On" else 0.0
    if key in enums:
        try:
            return enums[key].index(text) / (len(enums[key]) - 1)
        except ValueError as exc:
            raise PresetFormatError(f"cannot read {key}") from exc
    if key in {"InputLevel", "OutputLevel"}:
        db = max(-54.0, min(54.0, _permut8_number(text)))
        return 0.5 + 0.5 * math.copysign(math.sqrt(abs(db) / 54.0), db)
    if key == "FilterFreq":
        if text.startswith("---"):
            return 0.5
        frequency = max(_permut8_number(text[2:]), 1e-9)
        if text.startswith("LP"):
            return clamp(0.5 * math.log(frequency / 40.0) / math.log(600.0))
        if text.startswith("HP"):
            return clamp(0.5 + 0.5 * math.log(frequency / 20.0) / math.log(600.0))
        raise PresetFormatError("cannot read FilterFreq")
    if key == "FeedbackAmount":
        return clamp(_permut8_number(text) / 110.0)
    if key == "Mix":
        return clamp(_permut8_number(text) / 100.0)
    if key == "ClockFreq":
        if text.endswith("kHz"):
            frequency = _permut8_number(text)
            return (clamp(frequency / 88.2) if frequency <= 44.1
                    else clamp(0.5 + 0.5 * math.log(frequency / 44.1) / math.log(8.0)))
        base = text.rstrip("DT")
        steps = ["8/1", "4/1", "2/1", "1/1", "1/2", "1/4", "1/8"]
        if base not in steps:
            raise PresetFormatError("cannot read ClockFreq")
        return steps.index(base) / 6.0
    if key.startswith("Operand"):
        try:
            return int(text, 16) / 255.0
        except ValueError as exc:
            raise PresetFormatError(f"cannot read {key}") from exc
    raise PresetFormatError(f"cannot read {key}")


_PERMUT8_KEYS = [
    "InputLevel", "Limiter", "FilterFreq", "FilterPlacement",
    "FeedbackAmount", "FeedbackFlip", "FeedbackInvert", "OutputLevel",
    "Mix", "ClockFreq", "SyncMode", "Reverse", "Operator1",
    "Operand1.High", "Operand1.Low", "Operator2", "Operand2.High",
    "Operand2.Low",
]


def permut8_with_bank(current_state: bytes, bank: bytes, program: int = -1) -> bytes:
    sc = _sc_unpack(current_state, "NuPr", "Permut8", ".p8bank")
    current, programs = _parse_permut8_bank(bank)
    body = sc.body
    if len(body) < 20:
        raise PresetFormatError("unexpected Permut8 state layout")
    position = 20
    for _ in range(_le32(body, 16)):
        if position + 8 > len(body):
            raise PresetFormatError("unexpected Permut8 state layout")
        position += 8 + _le32(body, position + 4) + 1 + 72
    if position > len(body):
        raise PresetFormatError("unexpected Permut8 state layout")
    new_body = bytearray(body[:16])
    _put32(new_body, 30)
    for entry in sorted(programs, key=lambda item: _permut8_slot(item.slot)):
        name = entry.name[:24]
        while name and (ord(name[-1]) & 0xC0) == 0x80:
            name = name[:-1]
        if name and ord(name[-1]) >= 0xC0:
            name = name[:-1]
        _put32(new_body, 0x39685AB6)
        _put32(new_body, len(name))
        new_body.extend(name.encode("utf-8"))
        new_body.append(1 if entry.modified else 0)
        for key in _PERMUT8_KEYS:
            value = entry.fields.get(key)
            if value is None:
                raise PresetFormatError(f"program {entry.slot}: cannot read {key}")
            normalized = _permut8_normalize(key, value)
            new_body.extend(struct.pack("<f", normalized))
    new_body.extend(body[position:])
    selected = program if program >= 0 else (
        _permut8_slot(current) if len(current) == 2 else 0)
    return _sc_pack(sc, bytes(new_body), max(0, min(29, selected)))


# ---------------------------------------------------------------------------
# VST2 program/bank chunks and Full Bucket / OB-Xd banks
# ---------------------------------------------------------------------------

def is_vst2_chunk(data: bytes) -> bool:
    return len(data) >= 28 and data[:4] == b"CcnK"


def fxp_chunk(data: bytes) -> bytes:
    """Return the opaque state chunk of a VST2 ``.fxp`` program or ``.fxb`` bank."""
    if not is_vst2_chunk(data):
        raise PresetFormatError("not a VST2 .fxp/.fxb file")
    magic = data[8:12]
    if magic == b"FPCh":
        size_at = 56
    elif magic == b"FBCh":
        size_at = 156
    elif magic in {b"FxCk", b"FxBk"}:
        raise PresetFormatError(
            f"{magic.decode('ascii')} is a VST2 parameter-list preset, not a state chunk")
    else:
        raise PresetFormatError(f"unknown .fxp/.fxb type {magic!r}")
    if len(data) < size_at + 4:
        raise PresetFormatError("truncated .fxp/.fxb file")
    size = _be32(data, size_at)
    if size_at + 4 + size > len(data):
        raise PresetFormatError("truncated .fxp/.fxb file")
    return data[size_at + 4:size_at + 4 + size]


def is_full_bucket_bank(data: bytes) -> bool:
    return (len(data) >= 172 and data[:4] == b"CcnK" and data[8:12] == b"FBCh"
            and data[160:164] == b"tffp")


def full_bucket_bank_programs(data: bytes) -> list[tuple[str, bytes]]:
    """Split a Full Bucket Music bank into ``(name, program state)`` entries."""
    if not is_full_bucket_bank(data):
        return []
    end = min(len(data), 160 + _be32(data, 156))

    def entry(at: int) -> tuple[str, int] | None:
        if at + 4 > end:
            return None
        size = _le32(data, at)
        if size > 256 or at + 4 + size + 1 > end or data[at + 4 + size] != 1:
            return None
        return _decode_text(data[at + 4:at + 4 + size]), at + 4 + size + 1

    first = entry(168)
    if first is None:
        return []
    name, program = first
    cursor = program
    while cursor < end and 0x20 <= data[cursor] < 0x7F:
        cursor += 1
    version = data[program:cursor]
    if len(version) < 3 or b"_" not in version:
        return []
    programs: list[tuple[str, bytes]] = []
    while True:
        next_start = end
        next_program = 0
        next_name = ""
        search = program + len(version)
        while search + len(version) <= end and next_start == end:
            if data[search:search + len(version)] != version:
                search += 1
                continue
            for shift in range(0, 65):
                candidate = search - 1 - shift - 4
                if candidate < program + shift + 5:
                    continue
                found = entry(candidate)
                if found is not None and found[1] == search:
                    next_start = candidate
                    next_program = search
                    next_name = found[0]
                    break
            search += 1
        programs.append((name.strip(), data[program:next_start]))
        if next_start == end:
            break
        program = next_program
        name = next_name
    return programs


def _obxd_root(xml: str) -> int:
    positions = [position for position in (xml.find("<discoDSP"), xml.find("<Datsounds"))
                 if position >= 0]
    return min(positions) if positions else -1


def _obxd_bank_xml(data: bytes) -> str | None:
    if (len(data) < 168 or data[:4] != b"CcnK" or data[8:12] != b"FBCh"
            or data[160:164] != b"VC2!"):
        return None
    size = _le32(data, 164)
    if 168 + size > len(data):
        return None
    xml = _decode_text(data[168:168 + size]).rstrip("\x00")
    return xml if _obxd_root(xml) >= 0 else None


def _xml_unescape(text: str) -> str:
    for source, target in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                           ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(source, target)
    return text


def obxd_bank_programs(data: bytes) -> list[str]:
    xml = _obxd_bank_xml(data)
    if xml is None:
        return []
    names = []
    position = 0
    while True:
        position = xml.find("<program ", position)
        if position < 0:
            break
        end = xml.find(">", position)
        match = xml.find('programName="', position)
        name = ""
        if match >= 0 and (end < 0 or match < end):
            begin = match + 13
            name = _xml_unescape(xml[begin:xml.find('"', begin)]).rstrip(" ")
        names.append(name or f"Program {len(names) + 1}")
        position += 1
    return names


def obxd_bank_state(data: bytes, program: int) -> bytes:
    xml = _obxd_bank_xml(data)
    if xml is None:
        raise PresetFormatError("is not an OB-Xd bank")
    root = _obxd_root(xml)
    current = xml.find('currentProgram="', root)
    if current < 0 or current > xml.find(">", root):
        raise PresetFormatError("OB-Xd bank without a current program")
    begin = current + 16
    xml = xml[:begin] + str(program) + xml[xml.find('"', begin):]
    return juce_xml_blob(xml)


# ---------------------------------------------------------------------------
# Bank addressing: <file>#<index_or_name>
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BankProgram:
    index: int
    name: str


def split_bank_reference(reference: str | Path) -> tuple[str, str | None]:
    """Split ``cartridge.syx#14`` into its file and program selector."""
    text = str(reference)
    for extension in (".syx", ".fxb", ".p8bank"):
        marker = text.lower().rfind(extension + "#")
        if marker >= 0:
            return text[:marker + len(extension)], text[marker + len(extension) + 1:]
    return text, None


def bank_programs(path: str | Path, data: bytes | None = None) -> list[BankProgram]:
    """List the programs addressable inside a bank file."""
    bank_path = Path(path)
    if data is None:
        try:
            if bank_path.stat().st_size > _MAX_FILE_BYTES:
                return []
            data = bank_path.read_bytes()
        except OSError:
            return []
    suffix = bank_path.suffix.lower()
    if suffix == ".syx" and is_dx7_cartridge(data):
        return [BankProgram(index, name) for index, name in enumerate(dx7_voice_names(data))]
    if suffix == ".p8bank" and is_permut8_bank(data):
        return [BankProgram(index, name) for index, name in enumerate(permut8_bank_programs(data))]
    if suffix == ".fxb":
        names = obxd_bank_programs(data)
        if not names:
            names = [name for name, _program in full_bucket_bank_programs(data)]
        return [BankProgram(index, name) for index, name in enumerate(names)]
    return []


def resolve_bank_program(programs: list[BankProgram], selector: str | int) -> BankProgram:
    """Resolve an index or a case-insensitive program name."""
    if isinstance(selector, int):
        if 0 <= selector < len(programs):
            return programs[selector]
        raise PresetFormatError(
            f"program index {selector} is outside this bank (0-{len(programs) - 1})")
    text = str(selector).strip()
    if text.lstrip("+-").isdigit():
        index = int(text)
        if 0 <= index < len(programs):
            return programs[index]
        raise PresetFormatError(
            f"program index {index} is outside this bank (0-{len(programs) - 1})")
    folded = text.casefold()
    for program in programs:
        if program.name.casefold() == folded:
            return program
    raise PresetFormatError(f"this bank has no program named {text!r}")


# ---------------------------------------------------------------------------
# Format detection, summaries and one-shot decoding
# ---------------------------------------------------------------------------

def detect_format(path: str | Path | None, data: bytes = b"") -> str:
    """Return the decoder name for a preset file, sniffing its content."""
    suffix = Path(path).suffix.lower() if path else ""
    if is_xfer_json(data):
        return "serumfx" if is_serum_fx_file(data) else "serum"
    if data[:4] == b"VST3":
        return "vstpreset"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"NIKS":
        return "nksf"
    if is_dx7_cartridge(data):
        return "dx7"
    if is_vst2_chunk(data):
        return "fxp"
    if is_synplant_patch(data):
        return "synplant"
    if is_echobode_patch(data):
        return "echobode"
    if is_permut8_bank(data):
        return "permut8"
    if is_cherry_preset(data):
        return "cherry"
    if suffix == ".sbset":
        return "soundbox"
    if suffix == ".dspreset":
        return "decentsampler"
    if suffix == ".odin":
        return "juce-valuetree"
    if suffix == ".vvp":
        return "juce-xml"
    if suffix == ".vital":
        return "juce-string"
    if suffix == ".h2p" or data[:6] in {b"/*@Met", b"#AM="}:
        return "h2p"
    if suffix in {".serumpreset"}:
        return "serum"
    if suffix in {".serumfx", ".serumfxrack"}:
        return "serumfx"
    if suffix in {".dco106preset", ".mg1preset", ".sempreset", ".voltagepreset"}:
        return "cherry"
    if suffix in {".nksf", ".nksfx"}:
        return "nksf"
    if suffix in {".synplant"}:
        return "synplant"
    if suffix in {".echobode"}:
        return "echobode"
    if suffix in {".p8bank"}:
        return "permut8"
    if suffix in {".syx"}:
        return "dx7"
    if suffix in {".fxp", ".fxb"}:
        return "fxp"
    if suffix == ".dspreset":
        return "decentsampler"
    return "raw"


_DISPLAY_EXTENSIONS = {
    ".serumpreset": ".SerumPreset",
    ".serumfx": ".SerumFX",
    ".serumfxrack": ".SerumFXRack",
}


@dataclass
class DecodedPreset:
    """A preset decoded into component state bytes and display metadata."""

    format: str
    state: bytes | None = None
    controller_state: bytes | None = None
    name: str = ""
    category: str = ""
    bank: str = ""
    vendor: str = ""
    magic: str = ""
    uid: str = ""
    program_index: int | None = None
    program_name: str = ""
    warnings: list[str] = field(default_factory=list)
    requires_base_state: bool = False

    @property
    def loadable(self) -> bool:
        return bool(self.state)


def _preset_name(path: Path | None) -> str:
    return path.stem if path is not None else ""


def describe_preset(path: str | Path, data: bytes | None = None) -> DecodedPreset:
    """Read display metadata without necessarily decoding full component state."""
    preset_path = Path(path)
    if data is None:
        try:
            if preset_path.stat().st_size > _MAX_FILE_BYTES:
                raise PresetFormatError("preset exceeds the maximum supported size")
            data = preset_path.read_bytes()
        except OSError as exc:
            raise PresetFormatError(f"cannot read preset: {exc}") from exc
    format_name = detect_format(preset_path, data)
    result = DecodedPreset(format=format_name, name=_preset_name(preset_path))
    if format_name == "nksf":
        nks = parse_nks(data)
        result.name = nks.name or result.name
        result.vendor = nks.vendor
        result.category = nks.category
        result.bank = nks.bank
        result.magic = nks.magic
        result.uid = nks.uid
        result.state = nks.state
        if result.state is None:
            raise PresetFormatError("NKS file has no PCHK plugin chunk")
        return result
    if format_name == "dx7":
        if not is_dx7_cartridge(data):
            raise PresetFormatError("not a DX7 32-voice cartridge (4104-byte sysex)")
        result.bank = _preset_name(preset_path)
        names = dx7_voice_names(data)
        result.program_name = names[0] if names else ""
        result.name = result.program_name or result.name
        result.state = dexed_with_voice(None, data, 0)
        return result
    if format_name == "p8bank":
        names = permut8_bank_programs(data)
        result.bank = _preset_name(preset_path)
        result.program_name = names[0] if names else ""
        result.name = result.program_name or result.name
        result.requires_base_state = True
        return result
    if format_name == "fxp":
        if not is_vst2_chunk(data):
            raise PresetFormatError("not a VST2 .fxp/.fxb file")
        result.name = _preset_name(preset_path)
        if preset_path.suffix.lower() == ".fxb":
            programs = bank_programs(preset_path, data)
            if programs:
                result.bank = _preset_name(preset_path)
                result.program_name = programs[0].name
                result.name = result.program_name
                result.state = _decode_fxb_program(data, 0)
                return result
        result.state = fxp_chunk(data)
        return result
    if format_name == "serum":
        _header, payload = _xfer_payload(data)
        result.name = str(payload.get("presetName") or result.name)
        result.vendor = "Xfer Records"
        return result
    if format_name == "serumfx":
        result.vendor = "Xfer Records"
        result.requires_base_state = True
        return result
    if format_name in {"cherry", "synplant", "echobode", "permut8"}:
        result.requires_base_state = True
        return result
    if format_name == "h2p":
        result.name = _preset_name(preset_path)
        result.state = data
        return result
    if format_name in {"juce-valuetree", "juce-xml", "decentsampler", "soundbox",
                       "juce-string", "raw"}:
        result.state = _decode_standalone(format_name, data, preset_path)
        if format_name == "raw":
            result.requires_base_state = True
        return result
    return result


def _decode_standalone(format_name: str, data: bytes,
                       path: Path) -> bytes | None:
    if format_name == "juce-valuetree":
        return value_tree_to_juce_xml(data)
    if format_name == "juce-xml":
        return juce_xml_state(data, preset_path=path)
    if format_name == "decentsampler":
        return decent_sampler_state(data, path)
    if format_name == "soundbox":
        return soundbox_state(data)
    if format_name == "juce-string":
        return juce_string_state(data)
    return None


def _decode_fxb_program(data: bytes, index: int) -> bytes:
    if is_full_bucket_bank(data):
        programs = full_bucket_bank_programs(data)
        if 0 <= index < len(programs):
            return programs[index][1]
        raise PresetFormatError(
            f"this bank has no program {index} (0-{len(programs) - 1})")
    if obxd_bank_programs(data):
        return obxd_bank_state(data, index)
    return fxp_chunk(data)


def decode_preset(
    path: str | Path | None = None,
    data: bytes | None = None,
    *,
    format: str = "",
    selector: str | int | None = None,
    base_state: bytes | None = None,
) -> DecodedPreset:
    """Decode a preset file into the component state its plugin loads.

    ``selector`` addresses one program inside a bank file (``.syx``, ``.fxb``,
    ``.p8bank``), either by index or by name. ``base_state`` is the plugin's
    current component state, required by formats that only patch an existing
    state (Cherry Audio, Sonic Charge, Serum FX racks).
    """
    preset_path = Path(path) if path is not None else None
    if data is None:
        if preset_path is None:
            raise PresetFormatError("a preset path or its bytes are required")
        try:
            if preset_path.stat().st_size > _MAX_FILE_BYTES:
                raise PresetFormatError("preset exceeds the maximum supported size")
            data = preset_path.read_bytes()
        except OSError as exc:
            raise PresetFormatError(f"cannot read preset: {exc}") from exc
    format_name = format or detect_format(preset_path, data)
    result = DecodedPreset(format=format_name, name=_preset_name(preset_path))
    original_name = result.name

    if format_name == "nksf":
        nks = parse_nks(data)
        if nks.state is None:
            raise PresetFormatError("NKS file has no PCHK plugin chunk")
        result.state = nks.state
        result.name = nks.name or original_name
        result.vendor = nks.vendor
        result.category = nks.category
        result.bank = nks.bank
        result.magic = nks.magic
        result.uid = nks.uid
        return result

    if format_name == "serum":
        processor, controller = serum_preset_to_states(data)
        result.state = processor
        result.controller_state = controller
        result.vendor = "Xfer Records"
        return result

    if format_name == "serumfx":
        if not base_state:
            result.requires_base_state = True
            raise PresetFormatError(
                "a Serum FX rack preset needs the plugin's current state as a template")
        result.state = serum_fx_with_file(base_state, data)
        return result

    if format_name == "dx7":
        if not is_dx7_cartridge(data):
            raise PresetFormatError("not a DX7 32-voice cartridge (4104-byte sysex)")
        programs = bank_programs(preset_path or Path("cartridge.syx"), data)
        selected = resolve_bank_program(programs, selector) if selector is not None else programs[0]
        result.state = dexed_with_voice(base_state, data, selected.index)
        result.bank = _preset_name(preset_path)
        result.program_index = selected.index
        result.program_name = selected.name
        result.name = selected.name or original_name
        return result

    if format_name == "permut8":
        if not is_permut8_bank(data):
            raise PresetFormatError("not a Permut8 bank")
        names = permut8_bank_programs(data)
        index = -1
        if selector is not None:
            selected = resolve_bank_program(
                [BankProgram(i, name) for i, name in enumerate(names)], selector)
            index = selected.index
        if not base_state:
            result.requires_base_state = True
            result.bank = _preset_name(preset_path)
            if index >= 0:
                result.program_index = index
                result.program_name = names[index]
                result.name = names[index]
            raise PresetFormatError(
                "a Permut8 bank needs the plugin's current state as a template")
        result.state = permut8_with_bank(base_state, data, index)
        return result

    if format_name == "synplant":
        if not is_synplant_patch(data):
            raise PresetFormatError("not a Synplant patch")
        if not base_state:
            result.requires_base_state = True
            raise PresetFormatError(
                "a Synplant patch needs the plugin's current state as a template")
        result.state = synplant_with_patch(base_state, data, original_name)
        return result

    if format_name == "echobode":
        if not is_echobode_patch(data):
            raise PresetFormatError("not an Echobode patch")
        if not base_state:
            result.requires_base_state = True
            raise PresetFormatError(
                "an Echobode patch needs the plugin's current state as a template")
        result.state = echobode_with_patch(base_state, data, original_name)
        return result

    if format_name == "cherry":
        if not is_cherry_preset(data):
            raise PresetFormatError("not a Cherry Audio preset")
        result.state = cherry_with_preset(base_state, data)
        return result

    if format_name == "fxp":
        if not is_vst2_chunk(data):
            raise PresetFormatError("not a VST2 .fxp/.fxb file")
        suffix = preset_path.suffix.lower() if preset_path else ""
        if suffix == ".fxb":
            programs = bank_programs(preset_path, data) if preset_path else []
            if programs:
                selected = resolve_bank_program(programs, selector) if selector is not None else programs[0]
                result.state = _decode_fxb_program(data, selected.index)
                result.bank = original_name
                result.program_index = selected.index
                result.program_name = selected.name
                result.name = selected.name or original_name
                return result
            if selector is not None:
                raise PresetFormatError("this .fxb bank has no addressable programs")
        result.state = fxp_chunk(data)
        return result

    if format_name == "h2p":
        result.state = data
        return result

    if format_name in {"juce-valuetree", "juce-xml", "decentsampler",
                       "soundbox", "juce-string"}:
        result.state = _decode_standalone(format_name, data, preset_path or Path("preset"))
        return result

    if format_name == "raw":
        result.state = data
        return result

    raise PresetFormatError(f"unknown preset format {format_name!r}")
