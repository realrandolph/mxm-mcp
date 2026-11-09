"""Decoders for third-party preset files into native VST3 component state."""

from __future__ import annotations

import ctypes, ctypes.util, hashlib, html, json, math, re, struct, subprocess, zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = (
    "PresetFormatError DecodedPreset SUPPORTED_EXTENSIONS BANK_EXTENSIONS detect_format describe_preset decode_preset "
    "split_bank_reference bank_programs resolve_bank_program parse_nks nks_plugin_chunk is_xfer_json read_xfer write_xfer "
    "serum_preset_to_states is_serum_fx_file serum_fx_with_file read_value_tree write_value_tree value_tree_to_xml "
    "value_tree_to_juce_xml juce_xml_blob juce_xml_state juce_string_state is_cherry_preset cherry_with_preset "
    "is_dx7_cartridge dx7_voice_names unpack_dx7_voice dexed_with_voice is_synplant_patch is_echobode_patch "
    "is_permut8_bank synplant_with_patch echobode_with_patch permut8_bank_programs permut8_with_bank is_vst2_chunk "
    "fxp_chunk is_full_bucket_bank full_bucket_bank_programs obxd_bank_programs obxd_bank_state soundbox_state "
    "decent_sampler_state encode_juce_base64 decode_juce_base64".split())

class PresetFormatError(ValueError):
    pass

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    ".vstpreset .h2p .uhe-preset .vital .nksf .nksfx .serumpreset .serumfx .serumfxrack .syx .fxp .fxb .odin .dspreset "
    ".sbset .vvp .dco106preset .mg1preset .sempreset .voltagepreset .synplant .echobode .p8bank".split())
BANK_EXTENSIONS: frozenset[str] = frozenset({".syx", ".fxb", ".p8bank"})
_MAX_NESTING, _MAX_ITEMS = 64, 1_000_000

def _le32(data: bytes, offset: int = 0) -> int: return int.from_bytes(data[offset:offset + 4], "little")
def _le64(data: bytes, offset: int = 0) -> int: return int.from_bytes(data[offset:offset + 8], "little")
def _be32(data: bytes, offset: int = 0) -> int: return int.from_bytes(data[offset:offset + 4], "big")
def _put32(out: bytearray, value: int) -> None: out.extend(int(value & 0xFFFFFFFF).to_bytes(4, "little"))
def _put64(out: bytearray, value: int) -> None: out.extend(int(value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little"))
def _put_be32(out: bytearray, value: int) -> None: out.extend(int(value & 0xFFFFFFFF).to_bytes(4, "big"))
def _put_be32_at(out: bytearray, offset: int, value: int) -> None: out[offset:offset + 4] = int(value & 0xFFFFFFFF).to_bytes(4, "big")
def _decode_text(raw: bytes) -> str: return raw.decode("utf-8", "replace")
def _strip_nul(raw: bytes) -> bytes: return raw.rstrip(b"\x00")

# --- zstd ---
_ZSTD: dict[str, Any] = {"lib": None, "done": False}

def _zstd_library() -> Any:
    if _ZSTD["done"]: return _ZSTD["lib"]
    _ZSTD["done"] = True
    if name := ctypes.util.find_library("zstd"):
        try:
            lib = _ZSTD["lib"] = ctypes.CDLL(name)
            lib.ZSTD_decompress.restype = lib.ZSTD_compress.restype = lib.ZSTD_compressBound.restype = ctypes.c_size_t
            lib.ZSTD_getErrorName.restype = ctypes.c_char_p
        except (OSError, AttributeError): pass
    return _ZSTD["lib"]

def _zstd_cli(args: list[str], data: bytes, error: str) -> bytes:
    try: proc = subprocess.run(["zstd", *args, "-q", "-c"], input=data, capture_output=True, check=False)
    except OSError as exc: raise PresetFormatError("zstd is unavailable for this preset") from exc
    if proc.returncode != 0: raise PresetFormatError(error)
    return proc.stdout

def _zstd_decompress(data: bytes, expected_size: int | None = None) -> bytes:
    lib = _zstd_library()
    if lib:
        cap = expected_size if expected_size and expected_size > 0 else max(len(data) * 16, 1 << 20)
        for _ in range(4):
            buf, src = ctypes.create_string_buffer(cap), ctypes.create_string_buffer(data, len(data))
            w = lib.ZSTD_decompress(buf, cap, src, len(data))
            if lib.ZSTD_isError(w): raise PresetFormatError(f"zstd decompression failed: {_decode_text(lib.ZSTD_getErrorName(w))}")
            if expected_size is None or w == expected_size: return buf.raw[:w]
            cap = max(int(w), cap * 2)
    out = _zstd_cli(["-d"], data, "zstd payload does not decompress")
    if expected_size is not None and len(out) != expected_size: raise PresetFormatError("zstd payload does not decompress")
    return out

def _zstd_compress(data: bytes, level: int = 3) -> bytes:
    lib = _zstd_library()
    if lib:
        cap = lib.ZSTD_compressBound(len(data))
        buf, src = ctypes.create_string_buffer(cap), ctypes.create_string_buffer(data, len(data))
        w = lib.ZSTD_compress(buf, cap, src, len(data), level)
        if not lib.ZSTD_isError(w): return buf.raw[:w]
    return _zstd_cli([f"-{max(1, level)}"], data, "zstd compression failed")

# --- msgpack (NKS) ---
def _msgpack_decode(data: bytes) -> Any:
    def read(pos: int, depth: int = 0) -> tuple[Any, int]:
        if depth > _MAX_NESTING or pos >= len(data): raise PresetFormatError("invalid NKS metadata")
        c, pos = data[pos], pos + 1
        if c <= 0x7F: return c, pos
        if c >= 0xE0: return c - 256, pos
        if 0x80 <= c <= 0x8F: return _map(pos, c & 0xF, depth)
        if 0x90 <= c <= 0x9F: return _arr(pos, c & 0xF, depth)
        if 0xA0 <= c <= 0xBF: l = c & 0x1F; return _decode_text(data[pos:pos + l]), pos + l
        if c == 0xC0: return None, pos
        if c in (0xC2, 0xC3): return c == 0xC3, pos
        if c in (0xC4, 0xC5, 0xC6):
            w = 1 << (c - 0xC4); l = int.from_bytes(data[pos:pos + w], "big"); return data[pos + w:pos + w + l], pos + w + l
        if c in (0xD9, 0xDA, 0xDB):
            w = {0xD9: 1, 0xDA: 2, 0xDB: 4}[c]; l = int.from_bytes(data[pos:pos + w], "big"); return _decode_text(data[pos + w:pos + w + l]), pos + w + l
        if c in (0xCC, 0xCD, 0xCE, 0xCF): w = 1 << (c - 0xCC); return int.from_bytes(data[pos:pos + w], "big"), pos + w
        if c in (0xD0, 0xD1, 0xD2, 0xD3): w = 1 << (c - 0xD0); return int.from_bytes(data[pos:pos + w], "big", signed=True), pos + w
        if c in (0xCA, 0xCB): fmt, w = (">f", 4) if c == 0xCA else (">d", 8); return struct.unpack(fmt, data[pos:pos + w])[0], pos + w
        if c in (0xDC, 0xDD): w = 2 if c == 0xDC else 4; return _arr(pos + w, int.from_bytes(data[pos:pos + w], "big"), depth)
        if c in (0xDE, 0xDF): w = 2 if c == 0xDE else 4; return _map(pos + w, int.from_bytes(data[pos:pos + w], "big"), depth)
        if c in (0xD4, 0xD5, 0xD6, 0xD7, 0xD8): return None, pos + 1 + {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}[c]
        if c in (0xC7, 0xC8, 0xC9): wb = {0xC7: 1, 0xC8: 2, 0xC9: 4}[c]; l = int.from_bytes(data[pos:pos + wb], "big"); return None, pos + wb + 1 + l
        raise PresetFormatError(f"unsupported NKS msgpack code 0x{c:02x}")

    def _arr(pos: int, count: int, depth: int) -> tuple[list[Any], int]:
        res: list[Any] = []
        for _ in range(count): val, pos = read(pos, depth + 1); res.append(val)
        return res, pos

    def _map(pos: int, count: int, depth: int) -> tuple[dict[Any, Any], int]:
        res: dict[Any, Any] = {}
        for _ in range(count): k, pos = read(pos, depth + 1); v, pos = read(pos, depth + 1); res[k] = v
        return res, pos

    return read(0)[0]

# --- CBOR (Serum 2) ---
def _cbor_decode(data: bytes) -> Any:
    def read(pos: int, depth: int = 0) -> tuple[Any, int]:
        if depth > _MAX_NESTING or pos >= len(data): raise PresetFormatError("truncated Serum CBOR payload")
        head, pos = data[pos], pos + 1
        major, add = head >> 5, head & 0x1F
        if add < 24: val, defn = add, True
        elif add in (24, 25, 26, 27): w = 1 << (add - 24); val, pos, defn = int.from_bytes(data[pos:pos + w], "big"), pos + w, True
        elif add == 31: val, defn = 0, False
        else: raise PresetFormatError("invalid Serum CBOR header")
        if major == 0: return val, pos
        if major == 1: return -1 - val, pos
        if major in (2, 3):
            if defn: res = data[pos:pos + val]; return (_decode_text(res) if major == 3 else res), pos + val
            items = []
            while pos < len(data) and data[pos] != 0xFF: it, pos = read(pos, depth + 1); items.append(it)
            return ("".join(items) if major == 3 else b"".join(items)), pos + 1
        if major in (4, 5):
            items = []
            if defn:
                for _ in range(val * (2 if major == 5 else 1)): it, pos = read(pos, depth + 1); items.append(it)
            else:
                while pos < len(data) and data[pos] != 0xFF: it, pos = read(pos, depth + 1); items.append(it)
                pos += 1
            return ({items[i]: items[i + 1] for i in range(0, len(items) - 1, 2)} if major == 5 else items), pos
        if major == 6: return read(pos, depth + 1)
        if major == 7:
            if add in (20, 21): return add == 21, pos
            if add in (22, 23): return None, pos
            if add in (25, 26, 27): fmt, w = {25: (">e", 2), 26: (">f", 4), 27: (">d", 8)}[add]; return struct.unpack(fmt, data[pos - w:pos])[0], pos
        raise PresetFormatError("unsupported Serum CBOR value")

    return read(0)[0]

def _cbor_encode_head(major: int, val: int) -> bytes:
    p = major << 5
    if val < 24: return bytes([p | val])
    w = 1 if val < 0x100 else (2 if val < 0x10000 else (4 if val < 0x100000000 else 8))
    return bytes([p | (24 if w == 1 else 25 if w == 2 else 26 if w == 4 else 27)]) + val.to_bytes(w, "big")

def _cbor_encode(val: Any) -> bytes:
    if val is None: return b"\xf6"
    if isinstance(val, bool): return b"\xf5" if val else b"\xf4"
    if isinstance(val, int): return _cbor_encode_head(0, val) if val >= 0 else _cbor_encode_head(1, -1 - val)
    if isinstance(val, float): return b"\xfb" + struct.pack(">d", val)
    if isinstance(val, str): raw = val.encode("utf-8"); return _cbor_encode_head(3, len(raw)) + raw
    if isinstance(val, (bytes, bytearray)): return _cbor_encode_head(2, len(val)) + bytes(val)
    if isinstance(val, (list, tuple)): return _cbor_encode_head(4, len(val)) + b"".join(_cbor_encode(i) for i in val)
    if isinstance(val, dict): return _cbor_encode_head(5, len(val)) + b"".join(_cbor_encode(k) + _cbor_encode(v) for k, v in val.items())
    raise PresetFormatError(f"cannot encode {type(val).__name__} as CBOR")

# --- NKS ---
@dataclass
class NksPreset:
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
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"NIKS": raise PresetFormatError("not a Native Instruments NKS file")
    off = 12
    while off + 8 <= len(data):
        cid, size = data[off:off + 4].decode("ascii", "replace"), _le32(data, off + 4)
        start, end = off + 8, off + 8 + size
        if end > len(data): raise PresetFormatError(f"truncated NKS chunk {cid}")
        yield cid, data[start:end]
        off = end + (size & 1)

def nks_plugin_chunk(data: bytes) -> bytes:
    for cid, payload in _riff_chunks(data):
        if cid == "PCHK":
            if len(payload) < 4: raise PresetFormatError("PCHK chunk is too small")
            return payload if payload.startswith((b"VST3", b"VC2!", b"CcnK", b"<", b"{")) else payload[4:]
    raise PresetFormatError("NKS file has no PCHK plugin chunk")

def _nks_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None

def parse_nks(data: bytes) -> NksPreset:
    res = NksPreset()
    try: res.state = nks_plugin_chunk(data)
    except PresetFormatError: pass
    for cid, payload in _riff_chunks(data):
        if cid in {"NISI", "PLID"} and len(payload) > 4:
            try: meta = _msgpack_decode(payload[4:])
            except PresetFormatError: meta = None
            if isinstance(meta, dict) and cid == "NISI":
                res.name, res.vendor = str(meta.get("name") or ""), str(meta.get("vendor") or "")
                bchain = meta.get("bankchain")
                if isinstance(bchain, list) and bchain:
                    res.bankchain = [str(x) for x in bchain if isinstance(x, str)]; res.bank = res.bankchain[0]
                    if len(res.bankchain) > 1: res.category = res.bankchain[1]
                types = meta.get("types")
                if isinstance(types, list) and types and isinstance(types[0], list):
                    txt = "/".join(str(x) for x in types[0] if isinstance(x, str))
                    if txt: res.category = txt
            elif isinstance(meta, dict):
                magic = _nks_int(meta.get("VST.magic"))
                if magic is not None: res.magic = f"{magic & 0xFFFFFFFF:08X}"
                uid = meta.get("VST3.uid")
                if isinstance(uid, list): res.uid = "".join(f"{x & 0xFFFFFFFF:08X}" for x in map(_nks_int, uid) if x is not None)
        if cid == "PCHK" and len(payload) >= 28 and payload[8:12] == b"133k":
            res.magic = "536D3169" if payload[12:16] == b"p1ms" else "536D7069" if payload[12:16] == b"lpms" else res.magic
    return res

# --- Serum 2 (Xfer) ---
def is_xfer_json(data: bytes) -> bool: return data[:9] == b"XferJson\x00"

def _xfer_header(data: bytes) -> tuple[bytes, dict[str, Any]]:
    if not is_xfer_json(data) or len(data) < 17: raise PresetFormatError("not an Xfer (Serum 2) preset")
    if 17 + (h_sz := _le64(data, 9)) > len(data): raise PresetFormatError("truncated Xfer header")
    try:
        h = json.loads(_decode_text(_strip_nul(data[17:17 + h_sz])))
        if isinstance(h, dict): return data[17 + h_sz:], h
    except (ValueError, UnicodeError): pass
    raise PresetFormatError("Xfer header is not a valid JSON object")

def _ubjson_decode(data: bytes) -> Any:
    def read(pos: int, depth: int = 0) -> tuple[Any, int]:
        if depth > _MAX_NESTING or pos >= len(data): raise PresetFormatError("invalid UBJSON payload")
        m, pos = chr(data[pos]), pos + 1
        if m in {"Z", "N"}: return None, pos
        if m in {"T", "F"}: return m == "T", pos
        if m in {"i", "U", "I", "l", "L"}:
            w = {"i": 1, "U": 1, "I": 2, "l": 4, "L": 8}[m]
            return int.from_bytes(data[pos:pos + w], "big", signed=m in {"i", "l", "L"}), pos + w
        if m in {"d", "D"}: w = 4 if m == "d" else 8; return struct.unpack(">f" if w == 4 else ">d", data[pos:pos + w])[0], pos + w
        if m == "C": return chr(data[pos]), pos + 1
        if m == "S":
            if data[pos:pos + 1] in {b"i", b"U", b"I", b"l", b"L"}:
                cnt, pos = read(pos, depth + 1); return _decode_text(data[pos:pos + cnt]), pos + cnt
            end = data.find(b"\x00", pos); end = len(data) if end < 0 else end
            return _decode_text(data[pos:end]), end + (end < len(data))
        if m in "[{":
            close = "]" if m == "[" else "}"
            if data[pos:pos + 1] in {b"i", b"U", b"I", b"l", b"L"}:
                cnt, pos = read(pos, depth + 1); vals = [read(pos, depth + 1)[0] for _ in range(int(cnt))]
                return (vals if m == "[" else {}), pos
            vals = []
            while pos < len(data) and chr(data[pos]) != close: it, pos = read(pos, depth + 1); vals.append(it)
            pos += 1
            return (vals if m == "[" else {vals[i]: vals[i + 1] for i in range(0, len(vals) - 1, 2)}), pos
        raise PresetFormatError(f"unsupported UBJSON marker {m!r}")

    return read(0)[0]

def _xfer_payload(data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    rest, header = _xfer_header(data)
    if len(rest) < 8: raise PresetFormatError("truncated Xfer payload")
    sz, ver = _le32(rest, 0), _le32(rest, 4)
    comp = rest[8:]
    raw = _zstd_decompress(comp, sz) if ver == 2 else (zlib.decompress(comp) if ver == 0 else None)
    if raw is None: raise PresetFormatError(f"unsupported Xfer payload version {ver}")
    payload = _cbor_decode(raw) if ver == 2 else _ubjson_decode(raw)
    if not isinstance(payload, dict): raise PresetFormatError("Xfer payload is not a map")
    return header, payload

def read_xfer(data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    return _xfer_payload(data)

def write_xfer(header: dict[str, Any], payload: dict[str, Any]) -> bytes:
    raw = _cbor_encode(payload); comp = _zstd_compress(raw, 3)
    txt = json.dumps(dict(header, hash=hashlib.md5(comp).hexdigest()), sort_keys=True, separators=(",", ":")).encode("utf-8")
    out = bytearray(b"XferJson\x00")
    _put64(out, len(txt)); out.extend(txt)
    _put32(out, len(raw)); _put32(out, 2); out.extend(comp)
    return bytes(out)

_CTL_ONLY = {"ClipPlayer", "Filter", "GranularOsc", "MultiSampleOsc", "Osc", "SerumGUI", "SpectralOsc", "WTOsc",
             "arpBankDisplayName", "clipBankDisplayName", "presetAuthor", "presetDescription", "presetName"}

def serum_preset_to_states(data: bytes) -> tuple[bytes | None, bytes | None]:
    if is_xfer_json(data):
        try:
            if _xfer_header(data)[1].get("component") == "processor": return data, None
        except Exception: pass
    header, payload = _xfer_payload(data)
    if header.get("component") == "processor": return data, None
    proc = {k: v for k, v in payload.items() if k != "fileType" and k not in _CTL_ONLY}
    ctl = {k: v for k, v in payload.items() if k != "fileType" and (k in _CTL_ONLY or (any(k.startswith(p) for p in ("ArpClip", "MidiClip", "FXRack", "VoicePanel", "LFO")) and "PointModBus" not in k))}
    proc["component"], ctl["component"], ctl["presetHasBeenEdited"] = "processor", "controller", False
    meta = {k: header[k] for k in ("product", "productVersion", "url", "vendor", "version") if k in header}
    proc.update(meta); ctl.update(meta)
    ctl_h = dict(meta, component="controller", **{k: header.get(k, "") for k in ("presetName", "presetAuthor", "presetDescription")})
    return write_xfer(dict(meta, component="processor"), proc), write_xfer(ctl_h, ctl)

def is_serum_fx_file(data: bytes) -> bool:
    if not is_xfer_json(data) or len(data) < 17: return False
    try:
        h = json.loads(_decode_text(_strip_nul(data[17:17 + _le64(data, 9)])))
        return isinstance(h, dict) and h.get("fileType") in {"SerumFX", "SerumFXRack"}
    except Exception: return False

def serum_fx_with_file(current_state: bytes, file_data: bytes) -> bytes:
    _, file_payload = _xfer_payload(file_data)
    current_header, current_payload = _xfer_payload(current_state)
    if current_header.get("component") != "processor" or "FXRack0" not in current_payload: raise PresetFormatError("a Serum FX file loads into Serum 2 FX only")
    ft = current_header.get("fileType", file_payload.get("fileType", ""))
    if ft == "SerumFXRack":
        rack = file_payload.get("FXRack")
        if not isinstance(rack, dict): raise PresetFormatError("the Serum rack file has no FXRack")
    elif ft == "SerumFX":
        skip = {"fileType", "fxType", "product", "productVersion", "url", "vendor", "version", "hash"}
        rack = {"FX": [{k: v for k, v in file_payload.items() if k not in skip}]}
    else: raise PresetFormatError("is not a Serum FX or FX rack file")
    tgt = current_payload.setdefault("FXRack0", {})
    tgt["FX"] = rack.get("FX", [])
    if "plainParams" in rack and rack["plainParams"] != "default": tgt["plainParams"] = rack["plainParams"]
    if "parameters" in rack: tgt["parameters"] = rack["parameters"]
    current_header.pop("hash", None)
    return write_xfer(current_header, current_payload)

# --- JUCE ValueTree & XML ---
class _Reader:
    __slots__ = ("data", "index", "ok")

    def __init__(self, data: bytes) -> None: self.data, self.index, self.ok = data, 0, True

    def byte(self) -> int:
        if self.index >= len(self.data): self.ok = False; return 0
        v = self.data[self.index]; self.index += 1; return v

    def cint(self) -> int:
        h = self.byte(); cnt = h & 0x7F
        if cnt > 8: self.ok = False; return 0
        val = sum(self.byte() << (8 * s) for s in range(cnt))
        return -val if h & 0x80 else val

    def cstr(self) -> str:
        s = self.index
        end = self.data.find(b"\x00", s)
        if end < 0:
            self.ok, self.index = False, len(self.data)
            return _decode_text(self.data[s:])
        self.index = end + 1
        return _decode_text(self.data[s:end])

def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;"))

def _var_text(p: bytes) -> str:
    if not p: return ""
    vt, p = p[0], p[1:]
    if vt in (1, 6): return str(int.from_bytes(p[:4 if vt == 1 else 8], "little", signed=True))
    if vt in (2, 3): return "1" if vt == 2 else "0"
    if vt == 4: return format(struct.unpack("<d", p[:8])[0], ".17g")
    if vt == 5: return _decode_text(p).rstrip("\x00")
    if vt == 9: return ""
    raise PresetFormatError(f"ValueTree value type {vt} (array or binary) is not supported")

def juce_xml_blob(xml: str) -> bytes:
    body = xml.encode("utf-8") + b"\x00"; return b"VC2!" + len(body).to_bytes(4, "little") + body

@dataclass
class ValueTreeNode:
    type: str
    props: list[tuple[str, bytes]] = field(default_factory=list)
    kids: list["ValueTreeNode"] = field(default_factory=list)

    def child(self, name: str) -> "ValueTreeNode | None": return next((k for k in self.kids if k.type == name), None)

    def set_prop(self, name: str, value: bytes) -> None:
        for idx, (k, _) in enumerate(self.props):
            if k == name: self.props[idx] = (name, value); return
        self.props.append((name, value))

    def set_props(self, source: "ValueTreeNode") -> None:
        for k, v in source.props: self.set_prop(k, v)

def _read_value_tree(r: _Reader, depth: int = 0) -> ValueTreeNode:
    name = r.cstr()
    if not r.ok or not name or depth > _MAX_NESTING: raise PresetFormatError("not a JUCE ValueTree")
    props = r.cint()
    if props < 0 or props > _MAX_ITEMS: raise PresetFormatError("not a JUCE ValueTree")
    node = ValueTreeNode(name)
    for _ in range(props):
        k, sz = r.cstr(), r.cint()
        if not r.ok or sz < 0 or r.index + sz > len(r.data): raise PresetFormatError("truncated ValueTree property")
        node.props.append((k, r.data[r.index:r.index + sz])); r.index += sz
    kids = r.cint()
    if not r.ok or kids < 0 or kids > _MAX_ITEMS: raise PresetFormatError("not a JUCE ValueTree")
    node.kids = [_read_value_tree(r, depth + 1) for _ in range(kids)]
    return node

def read_value_tree(data: bytes) -> ValueTreeNode: return _read_value_tree(_Reader(data))

def _node_xml(n: ValueTreeNode) -> str:
    attrs = "".join(f' {k}="{_xml_escape(_var_text(v))}"' for k, v in n.props)
    if not n.kids: return f"<{n.type}{attrs}/>"
    return f"<{n.type}{attrs}>" + "".join(map(_node_xml, n.kids)) + f"</{n.type}>"

def value_tree_to_xml(data: bytes) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n\n' + _node_xml(read_value_tree(data)) + "\n"

def value_tree_to_juce_xml(data: bytes) -> bytes: return juce_xml_blob(value_tree_to_xml(data))

def _write_cint(out: bytearray, val: int) -> None:
    if val == 0: out.append(0); return
    neg, raw, buf = val < 0, abs(val), bytearray()
    while raw: buf.append(raw & 0xFF); raw >>= 8
    out.append(len(buf) | (0x80 if neg else 0)); out.extend(buf)

def write_value_tree(node: ValueTreeNode) -> bytes:
    out = bytearray()

    def emit(n: ValueTreeNode) -> None:
        out.extend(n.type.encode("utf-8") + b"\x00"); _write_cint(out, len(n.props))
        for k, v in n.props:
            out.extend(k.encode("utf-8") + b"\x00"); _write_cint(out, len(v)); out.extend(v)
        _write_cint(out, len(n.kids))
        for kid in n.kids: emit(kid)

    emit(node)
    return bytes(out)

def juce_xml_state(xml: bytes | str, *, preset_path: str | Path | None = None) -> bytes:
    txt = (xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml).rstrip("\x00 \t\r\n")
    root, ver = txt.find("<VASTvaporizer2 "), txt.find('PatchVersion="VASTVaporizerParamsV2.00000"')
    if root >= 0 and 0 <= ver < txt.find(">", root): txt = txt[:ver + 36] + "1" + txt[ver + 37:]
    if not txt.lstrip().startswith("<?xml"): txt = '<?xml version="1.0" encoding="UTF-8"?>\n' + txt
    return juce_xml_blob(txt)

def juce_string_state(data: bytes) -> bytes: return data if data.endswith(b"\x00") else data + b"\x00"

def decent_sampler_state(data: bytes, preset_path: str | Path) -> bytes:
    txt = _decode_text(data)
    if (root := txt.find("<DecentSampler")) < 0: raise PresetFormatError("not a DecentSampler preset (.dspreset)")
    txt = txt[:root + 14] + f' _samplePath="{_xml_escape(str(Path(preset_path).parent))}"' + txt[root + 14:]
    return juce_xml_blob(txt if txt.lstrip().startswith("<?xml") else '<?xml version="1.0" encoding="UTF-8"?>\n' + txt)

def soundbox_state(data: bytes) -> bytes:
    txt = _decode_text(data)
    if (start := txt.find("<Audiomodern.Soundbox_Preset")) < 0: raise PresetFormatError("not a Soundbox preset (.sbset)")
    txt = txt[start:].replace("<Audiomodern.Soundbox_Preset", '<Audiomodern.Soundbox_State preset=""', 1).replace("</Audiomodern.Soundbox_Preset>", "</Audiomodern.Soundbox_State>")
    return juce_xml_blob('<?xml version="1.0" encoding="UTF-8"?>\n' + txt)

# --- Cherry Audio ---
def is_cherry_preset(data: bytes) -> bool: return len(data) > 6 and data[:5] == b"main\x00"

def cherry_with_preset(current_state: bytes | None, preset_data: bytes) -> bytes:
    try: preset = read_value_tree(preset_data)
    except Exception as exc: raise PresetFormatError('not a Cherry Audio preset (JUCE ValueTree "main")') from exc
    if preset.type != "main": raise PresetFormatError('not a Cherry Audio preset (JUCE ValueTree "main")')
    disp = next((v for k, v in preset.props if k == "presetName"), None)
    if current_state and len(current_state) > 0xB0 and current_state[:4] == b"VstW" and current_state[16:20] == b"CcnK":
        chunk = current_state[0xB0:]; r = _Reader(chunk); root = _read_value_tree(r)
        pinfo = root.child("presetInfo")
        if pinfo is None: raise PresetFormatError("unexpected Voltage Modular state")
        tail = chunk[r.index:]
        pinfo.set_props(preset); pinfo.kids = list(preset.kids)
        if disp: pinfo.set_prop("displayName", disp)
        new_chunk, out = write_value_tree(root) + tail, bytearray(current_state[:0xB0])
        _put_be32_at(out, 20, len(out) - 24 + len(new_chunk)); _put_be32_at(out, 0xAC, len(new_chunk))
        out.extend(new_chunk); return bytes(out)
    if current_state:
        r = _Reader(current_state); root = _read_value_tree(r); tgt = root.child("pt")
        if tgt is None: raise PresetFormatError("a Cherry Audio preset loads into its own plugin only")
        tail = current_state[r.index:]
        tgt.set_props(preset)
        for child in preset.kids:
            ex = tgt.child(child.type)
            if child.type == "pd" and ex is not None: ex.set_props(child)
            elif ex is not None: tgt.kids[tgt.kids.index(ex)] = child
            else: tgt.kids.append(child)
        cur = root.child("curpreset")
        if cur is not None and disp: cur.set_prop("displayName", disp); cur.set_prop("name", disp)
        return write_value_tree(root) + tail
    pt = ValueTreeNode("pt", list(preset.props), list(preset.kids))
    cur = [("displayName", disp), ("name", disp)] if disp else []
    return write_value_tree(ValueTreeNode("savedState", [], [ValueTreeNode("curpreset", cur), pt]))

# --- DX7 ---
_JUCE_B64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"

def encode_juce_base64(data: bytes) -> str:
    v = int.from_bytes(data, "little")
    return f"{len(data)}." + "".join(_JUCE_B64[(v >> (6 * i)) & 63] for i in range((len(data) * 8 + 5) // 6))

def decode_juce_base64(text: str) -> bytes:
    dot = text.find(".")
    if dot < 0: return b""
    try: sz = int(text[:dot])
    except ValueError: return b""
    v = sum(max(0, _JUCE_B64.find(ch)) << (6 * i) for i, ch in enumerate(text[dot + 1:]))
    return (v & ((1 << (8 * sz)) - 1)).to_bytes(sz, "little")

def is_dx7_cartridge(data: bytes) -> bool:
    return len(data) == 4104 and data[0] == 0xF0 and data[1] == 0x43 and data[3:6] == b"\x09\x20\x00"

def dx7_voice_names(data: bytes) -> list[str]:
    return [_decode_text(data[6 + v * 128 + 118:6 + v * 128 + 128]).rstrip(" \x00") for v in range(32)] if is_dx7_cartridge(data) else []

def unpack_dx7_voice(rec: bytes) -> bytes:
    if len(rec) < 128: raise PresetFormatError("truncated DX7 voice")
    out = bytearray()
    for op in range(6):
        q = rec[op * 17:op * 17 + 17]
        out += q[:11] + bytes([q[11] & 3, (q[11] >> 2) & 3, q[12] & 7, q[13] & 3, (q[13] >> 2) & 7, q[14],
                               q[15] & 1, (q[15] >> 1) & 31, q[16], (q[12] >> 3) & 15])
    out += rec[102:110] + bytes([rec[110] & 31, rec[111] & 7, (rec[111] >> 3) & 1]) + rec[112:116]
    out += bytes([rec[116] & 1, (rec[116] >> 1) & 7, (rec[116] >> 4) & 7, rec[117]]) + rec[118:128]
    return bytes(out)

def _set_xml_attribute(xml: str, name: str, value: str) -> str:
    key = f' {name}="'; s = xml.find(key)
    if s < 0: return xml
    b = s + len(key); e = xml.find('"', b)
    return xml if e < 0 else xml[:b] + value + xml[e:]

def dexed_with_voice(current_state: bytes | None, cartridge: bytes, voice: int) -> bytes:
    if not is_dx7_cartridge(cartridge): raise PresetFormatError("a DX7 cartridge must be a 4104-byte 32-voice bulk dump")
    if not 0 <= voice <= 31: raise PresetFormatError("DX7 voices are numbered 0-31")
    if current_state:
        if len(current_state) < 9 or current_state[:4] != b"VC2!": raise PresetFormatError("a DX7 cartridge loads into Dexed only (its state is JUCE XML)")
        xml = _decode_text(current_state[8:]).rstrip("\x00")
        if "<dexedState" not in xml: raise PresetFormatError("a DX7 cartridge loads into Dexed only")
    else: xml = '<?xml version="1.0" encoding="UTF-8"?>\n\n<dexedState currentProgram="0" opSwitch="111111" base64:sysex="" base64:program=""/>'
    prog, m = bytearray(161), ' base64:program="'
    s = xml.find(m)
    if s >= 0:
        ex = decode_juce_base64(xml[s + len(m):xml.find('"', s + len(m))])
        prog[155:161] = ex[155:161]; prog.extend(b"\x00" * max(0, 161 - len(prog)))
    prog[:155] = unpack_dx7_voice(cartridge[6 + voice * 128:6 + (voice + 1) * 128])
    prog[155] = 0x3F
    for k, v in [("base64:sysex", encode_juce_base64(cartridge)), ("base64:program", encode_juce_base64(bytes(prog))), ("currentProgram", str(voice)), ("opSwitch", "111111")]:
        xml = _set_xml_attribute(xml, k, v)
    return juce_xml_blob(xml)

# --- Sonic Charge (Synplant / Echobode / Permut8) ---
_SC_MAGIC = b"\x59\xa2\xcd\x18"

def is_synplant_patch(data: bytes) -> bool: return data[:16] == b"SynplantPatch: {"
def is_echobode_patch(data: bytes) -> bool: return data[:16] == b"EchobodePatch: {"
def is_permut8_bank(data: bytes) -> bool: return data[:16] == b"Permut8BankV1: {"

@dataclass
class _ScState:
    prefix: bytes = b""
    head: bytes = b""
    vst3: bool = False
    wrapped: bool = False
    chunk_header: bytes = b""
    body: bytes = b""

def _sc_unpack(state: bytes, fxid: str, plugin: str, what: str) -> _ScState:
    res, off = _ScState(), 0
    if len(state) >= 10 and state[:4] == _SC_MAGIC: res.vst3, res.prefix, off = True, state[:10], 10
    c_at, c_sz = off, len(state) - off
    if len(state) >= off + 60 and state[off:off + 4] == b"CcnK":
        bank, prog = state[off + 8:off + 12] == b"FBCh", state[off + 8:off + 12] == b"FPCh"
        if (not bank and not prog) or state[off + 16:off + 20] != fxid.encode("ascii"): raise PresetFormatError(f"a {what} loads into {plugin} only")
        sz_at = off + (156 if bank else 56)
        res.wrapped, res.head = True, state[off:sz_at]; c_sz, c_at = _be32(state, sz_at), sz_at + 4
    chunk = state[c_at:c_at + c_sz]
    if len(chunk) < 8: raise PresetFormatError(f"unexpected {plugin} state layout")
    hdr = 16 if chunk[4:8] == b"mrAM" else 4
    if hdr == 16 and chunk[8:12] != fxid[::-1].encode("ascii"): raise PresetFormatError(f"a {what} loads into {plugin} only")
    bsz = _le32(chunk, hdr)
    try: body = zlib.decompress(chunk[hdr + 4:])
    except Exception as exc: raise PresetFormatError(f"{plugin} state does not decompress") from exc
    if len(body) != bsz: raise PresetFormatError(f"{plugin} state has an unexpected size")
    res.chunk_header, res.body = chunk[:hdr], body
    return res

def _sc_pack(sc: _ScState, body: bytes, current: int) -> bytes:
    nc = bytearray(sc.chunk_header); _put32(nc, len(body)); nc.extend(zlib.compress(body, 6))
    if not sc.wrapped: return bytes(nc)
    rest = bytearray(sc.head[8:]); _put_be32(rest, len(nc)); rest.extend(nc)
    fx = bytearray(b"CcnK"); _put_be32(fx, len(rest)); fx.extend(rest)
    if not sc.vst3: return bytes(fx)
    out = bytearray(_SC_MAGIC + b"\x00" + bytes([current & 0xFF])); _put32(out, len(fx)); out.extend(fx)
    return bytes(out)

def _sc_with_patch(state: bytes, patch: bytes, name: str, fxid: str, plugin: str, what: str) -> bytes:
    sc = _sc_unpack(state, fxid, plugin, what)
    body = sc.body
    single = len(body) >= 8 and _le32(body, 0) == len(body) - 4 and 0x41 <= body[4] <= 0x7A
    cnt = 0 if single else (_le32(body, 0) if len(body) >= 4 else 0)
    pos, patches = 4, []
    for _ in range(cnt):
        sz = _le32(body, pos); patches.append(body[pos + 4:pos + 4 + sz]); pos += 4 + sz
    trailer = b"" if single else body[pos:]
    if single: patches.append(b"")
    txt = _decode_text(patch).replace("\r", "")
    esc = name.replace("\\", "\\\\").replace('"', '\\"'); marker = txt.find('\n\tname: "')
    if marker >= 0: txt = txt[:marker + 1] + f'\tname: "{esc}"' + txt[txt.find("\n", marker + 1):]
    if not txt.endswith("\n"): txt += "\n"
    patches[0] = txt.encode("utf-8")
    new_b = bytearray()
    if not single: _put32(new_b, len(patches))
    for it in patches: _put32(new_b, len(it)); new_b.extend(it)
    new_b.extend(trailer)
    return _sc_pack(sc, bytes(new_b), 0)

def synplant_with_patch(current_state: bytes, patch: bytes, name: str) -> bytes:
    return _sc_with_patch(current_state, patch, name, "NuSP", "Synplant", ".synplant patch")

def echobode_with_patch(current_state: bytes, patch: bytes, name: str) -> bytes:
    return _sc_with_patch(current_state, patch, name, "NuEB", "Echobode", ".echobode patch")

@dataclass
class _Permut8Program:
    slot: str = ""
    name: str = ""
    modified: bool = False
    fields: dict[str, str] = field(default_factory=dict)

def _parse_permut8_bank(data: bytes) -> tuple[str, list[_Permut8Program]]:
    txt = _decode_text(data).replace("\r", "")
    if not txt.startswith("Permut8BankV1: {"): raise PresetFormatError("not a Permut8 bank")
    cur, progs, act = "", [], None
    for line in txt.splitlines():
        if line.startswith("\tCurrent Program: "): cur = line[18:].strip()
        elif len(line) == 7 and line.startswith("\t\t") and line[2] in "ABC" and line[3].isdigit() and line[4:7] == ": {":
            act = _Permut8Program(line[2:4]); progs.append(act)
        elif act is not None and line.startswith("\t\t}"): act = None
        elif act is not None and line.startswith("\t\t\t") and ": " in line:
            k, v = (s.strip() for s in line.split(": ", 1))
            if k == "Name": act.name = (v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v).replace('\\"', '"').replace("\\\\", "\\")
            elif k == "Modified": act.modified = v == "true"
            else: act.fields[k] = v
    if len(progs) != 30: raise PresetFormatError("not a Permut8 bank with 30 programs")
    return cur, progs

def permut8_bank_programs(data: bytes) -> list[str]:
    _, progs = _parse_permut8_bank(data)
    names = [""] * 30
    for p in progs: names[(ord(p.slot[0]) - ord("A")) * 10 + int(p.slot[1])] = p.name or p.slot
    return names

def _permut8_normalize(k: str, t: str) -> float:
    enums = {"FilterPlacement": ("Off", "Input", "Feedback", "Output"), "SyncMode": ("Off", "Dotted", "Triplets", "Standard"),
             "Operator1": ("NOP", "AND", "MUL", "OSC", "RND"), "Operator2": ("NOP", "OR", "XOR", "MSK", "SUB")}
    if k in {"Limiter", "FeedbackFlip", "FeedbackInvert", "Reverse"}: return 1.0 if t == "On" else 0.0
    if k in enums:
        try: return enums[k].index(t) / (len(enums[k]) - 1)
        except ValueError as exc: raise PresetFormatError(f"cannot read {k}") from exc
    s = next((i for i, c in enumerate(t) if c in "+-.0123456789"), len(t))
    try: num = float(t[s:])
    except ValueError: num = 0.0
    if k in {"InputLevel", "OutputLevel"}:
        db = max(-54.0, min(54.0, num)); return 0.5 + 0.5 * math.copysign(math.sqrt(abs(db) / 54.0), db)
    if k == "FilterFreq":
        return 0.5 if t.startswith("---") else max(0.0, min(1.0, 0.5 * math.log(max(num, 1e-9) / (40.0 if t.startswith("LP") else 20.0)) / math.log(600.0) + (0.0 if t.startswith("LP") else 0.5)))
    if k in {"FeedbackAmount", "Mix"}: return max(0.0, min(1.0, num / (110.0 if k == "FeedbackAmount" else 100.0)))
    if k == "ClockFreq":
        if t.endswith("kHz"): return max(0.0, min(1.0, num / 88.2 if num <= 44.1 else 0.5 + 0.5 * math.log(num / 44.1) / math.log(8.0)))
        steps = ("8/1", "4/1", "2/1", "1/1", "1/2", "1/4", "1/8")
        if t.rstrip("DT") in steps: return steps.index(t.rstrip("DT")) / 6.0
    if k.startswith("Operand"):
        try: return int(t, 16) / 255.0
        except ValueError as exc: raise PresetFormatError(f"cannot read {k}") from exc
    raise PresetFormatError(f"cannot read {k}")

_PERMUT8_KEYS = (
    "InputLevel Limiter FilterFreq FilterPlacement FeedbackAmount FeedbackFlip FeedbackInvert OutputLevel Mix ClockFreq "
    "SyncMode Reverse Operator1 Operand1.High Operand1.Low Operator2 Operand2.High Operand2.Low".split())

def permut8_with_bank(current_state: bytes, bank: bytes, program: int = -1) -> bytes:
    sc = _sc_unpack(current_state, "NuPr", "Permut8", ".p8bank")
    cur, progs = _parse_permut8_bank(bank)
    body, pos = sc.body, 20
    for _ in range(_le32(body, 16)): pos += 8 + _le32(body, pos + 4) + 1 + 72
    new_b = bytearray(body[:16]); _put32(new_b, 30)
    for entry in sorted(progs, key=lambda i: (ord(i.slot[0]) - ord("A")) * 10 + int(i.slot[1])):
        name = entry.name[:24].encode("utf-8")
        _put32(new_b, 0x39685AB6); _put32(new_b, len(name)); new_b.extend(name); new_b.append(1 if entry.modified else 0)
        for k in _PERMUT8_KEYS:
            v = entry.fields.get(k)
            if v is None: raise PresetFormatError(f"program {entry.slot}: cannot read {k}")
            new_b.extend(struct.pack("<f", _permut8_normalize(k, v)))
    new_b.extend(body[pos:])
    sel = program if program >= 0 else (((ord(cur[0]) - ord("A")) * 10 + int(cur[1])) if len(cur) == 2 else 0)
    return _sc_pack(sc, bytes(new_b), max(0, min(29, sel)))

# --- VST2 chunks & banks ---
def is_vst2_chunk(data: bytes) -> bool: return len(data) >= 28 and data[:4] == b"CcnK"

def fxp_chunk(data: bytes) -> bytes:
    if not is_vst2_chunk(data): raise PresetFormatError("not a VST2 .fxp/.fxb file")
    m = data[8:12]; sz_at = 56 if m == b"FPCh" else (156 if m == b"FBCh" else None)
    if sz_at is None: raise PresetFormatError(f"{m.decode('ascii', 'replace')} is not a supported state chunk")
    sz = _be32(data, sz_at)
    if sz_at + 4 + sz > len(data): raise PresetFormatError("truncated .fxp/.fxb file")
    return data[sz_at + 4:sz_at + 4 + sz]

def is_full_bucket_bank(data: bytes) -> bool:
    return len(data) >= 172 and data[:4] == b"CcnK" and data[8:12] == b"FBCh" and data[160:164] == b"tffp"

def full_bucket_bank_programs(data: bytes) -> list[tuple[str, bytes]]:
    if not is_full_bucket_bank(data): return []
    end = min(len(data), 160 + _be32(data, 156))

    def entry(at: int) -> tuple[str, int] | None:
        if at + 4 > end: return None
        sz = _le32(data, at)
        return (_decode_text(data[at + 4:at + 4 + sz]), at + 4 + sz + 1) if sz <= 256 and at + 5 + sz <= end and data[at + 4 + sz] == 1 else None

    first = entry(168)
    if not first: return []
    name, prog = first; cur = prog
    while cur < end and 0x20 <= data[cur] < 0x7F: cur += 1
    ver = data[prog:cur]
    if len(ver) < 3 or b"_" not in ver: return []
    progs: list[tuple[str, bytes]] = []
    while True:
        nxt_st, nxt_pr, nxt_nm, s = end, 0, "", prog + len(ver)
        while s + len(ver) <= end and nxt_st == end:
            if data[s:s + len(ver)] == ver:
                for sh in range(0, 65):
                    f = entry(s - 5 - sh)
                    if f and f[1] == s: nxt_st, nxt_pr, nxt_nm = s - 5 - sh, s, f[0]; break
            s += 1
        progs.append((name.strip(), data[prog:nxt_st]))
        if nxt_st == end: break
        prog, name = nxt_pr, nxt_nm
    return progs

def _obxd_bank_xml(data: bytes) -> str | None:
    if len(data) >= 168 and data[:4] == b"CcnK" and data[8:12] == b"FBCh" and data[160:164] == b"VC2!":
        sz = _le32(data, 164)
        if 168 + sz <= len(data):
            xml = _decode_text(data[168:168 + sz]).rstrip("\x00")
            if "<discoDSP" in xml or "<Datsounds" in xml: return xml
    return None

def obxd_bank_programs(data: bytes) -> list[str]:
    xml = _obxd_bank_xml(data)
    return [html.unescape(m).rstrip() for m in re.findall(r'<program\s+[^>]*programName="([^"]*)"', xml)] if xml else []

def obxd_bank_state(data: bytes, program: int) -> bytes:
    xml = _obxd_bank_xml(data)
    if not xml: raise PresetFormatError("is not an OB-Xd bank")
    pos = [xml.find(p) for p in ("<discoDSP", "<Datsounds") if xml.find(p) >= 0]
    cur = xml.find('currentProgram="', min(pos) if pos else -1)
    if cur < 0: raise PresetFormatError("OB-Xd bank without a current program")
    b = cur + 16
    return juce_xml_blob(xml[:b] + str(program) + xml[xml.find('"', b):])

# --- Bank Addressing ---
@dataclass(frozen=True)
class BankProgram:
    index: int
    name: str

def split_bank_reference(reference: str | Path) -> tuple[str, str | None]:
    t = str(reference)
    for ext in (".syx", ".fxb", ".p8bank"):
        m = t.lower().rfind(ext + "#")
        if m >= 0: return t[:m + len(ext)], t[m + len(ext) + 1:]
    return t, None

def bank_programs(path: str | Path, data: bytes | None = None) -> list[BankProgram]:
    bp = Path(path)
    if data is None:
        try: data = bp.read_bytes()
        except OSError: return []
    suf = bp.suffix.lower()
    if suf == ".syx" and is_dx7_cartridge(data): names = dx7_voice_names(data)
    elif suf == ".p8bank" and is_permut8_bank(data): names = permut8_bank_programs(data)
    elif suf == ".fxb": names = obxd_bank_programs(data) or [n for n, _ in full_bucket_bank_programs(data)]
    else: return []
    return [BankProgram(i, n) for i, n in enumerate(names)]

def resolve_bank_program(programs: list[BankProgram], selector: str | int) -> BankProgram:
    if isinstance(selector, int) or str(selector).strip().lstrip("+-").isdigit():
        idx = int(selector)
        if 0 <= idx < len(programs): return programs[idx]
        raise PresetFormatError(f"program index {idx} is outside this bank (0-{len(programs) - 1})")
    f = str(selector).strip().casefold()
    for p in programs:
        if p.name.casefold() == f: return p
    raise PresetFormatError(f"this bank has no program named {selector!r}")

# --- Detection, describe, decode ---
def detect_format(path: str | Path | None, data: bytes = b"") -> str:
    suf = Path(path).suffix.lower() if path else ""
    if is_xfer_json(data): return "serumfx" if is_serum_fx_file(data) else "serum"
    if data[:4] == b"VST3": return "vstpreset"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"NIKS": return "nksf"
    if is_dx7_cartridge(data) or suf == ".syx": return "dx7"
    if is_vst2_chunk(data) or suf in {".fxp", ".fxb"}: return "fxp"
    if is_synplant_patch(data) or suf == ".synplant": return "synplant"
    if is_echobode_patch(data) or suf == ".echobode": return "echobode"
    if is_permut8_bank(data) or suf == ".p8bank": return "permut8"
    if is_cherry_preset(data) or suf in {".dco106preset", ".mg1preset", ".sempreset", ".voltagepreset"}: return "cherry"
    fmts = {".sbset": "soundbox", ".dspreset": "decentsampler", ".odin": "juce-valuetree", ".vvp": "juce-xml", ".vital": "juce-string"}
    if suf in fmts: return fmts[suf]
    if suf == ".h2p" or data[:6] in {b"/*@Met", b"#AM="}: return "h2p"
    return "raw"

@dataclass
class DecodedPreset:
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
    def loadable(self) -> bool: return bool(self.state)

def _process_preset(path: str | Path | None = None, data: bytes | None = None, *, format: str = "", selector: str | int | None = None,
                    base_state: bytes | None = None, describe_only: bool = False) -> DecodedPreset:
    pp = Path(path) if path is not None else None
    if data is None:
        if pp is None: raise PresetFormatError("a preset path or its bytes are required")
        try: data = pp.read_bytes()
        except OSError as exc: raise PresetFormatError(f"cannot read preset: {exc}") from exc
    fmt = format or detect_format(pp, data); res = DecodedPreset(format=fmt, name=pp.stem if pp else ""); orig = res.name

    def require_base(what: str) -> None:
        if not describe_only and not base_state: raise PresetFormatError(f"{what} needs the plugin's current state as a template")

    if fmt == "nksf":
        nks = parse_nks(data)
        if nks.state is None: raise PresetFormatError("NKS file has no PCHK plugin chunk")
        res.state, res.name, res.vendor, res.category, res.bank, res.magic, res.uid = nks.state, nks.name or orig, nks.vendor, nks.category, nks.bank, nks.magic, nks.uid
        return res
    if fmt == "serum":
        res.vendor = "Xfer Records"
        if describe_only: res.name = str(_xfer_payload(data)[1].get("presetName") or res.name)
        else: res.state, res.controller_state = serum_preset_to_states(data)
        return res
    if fmt == "serumfx":
        res.vendor, res.requires_base_state = "Xfer Records", True
        require_base("a Serum FX rack preset")
        if not describe_only: res.state = serum_fx_with_file(base_state, data)
        return res
    if fmt == "dx7":
        if not is_dx7_cartridge(data): raise PresetFormatError("not a DX7 32-voice cartridge (4104-byte sysex)")
        progs = bank_programs(pp or Path("cartridge.syx"), data)
        sel = resolve_bank_program(progs, selector) if selector is not None else progs[0]
        res.bank, res.program_index, res.program_name, res.name = (pp.stem if pp else ""), sel.index, sel.name, sel.name or orig
        res.state = dexed_with_voice(base_state, data, sel.index)
        return res
    if fmt == "permut8":
        if not is_permut8_bank(data): raise PresetFormatError("not a Permut8 bank")
        names = permut8_bank_programs(data)
        idx = resolve_bank_program([BankProgram(i, n) for i, n in enumerate(names)], selector).index if selector is not None else -1
        res.bank, res.requires_base_state = (pp.stem if pp else ""), True
        if idx >= 0: res.program_index, res.program_name, res.name = idx, names[idx], names[idx]
        elif names: res.program_name = names[0]; res.name = res.program_name or res.name
        require_base("a Permut8 bank")
        if not describe_only: res.state = permut8_with_bank(base_state, data, idx)
        return res
    if fmt in {"synplant", "echobode"}:
        fn, chk = (synplant_with_patch, is_synplant_patch) if fmt == "synplant" else (echobode_with_patch, is_echobode_patch)
        if not chk(data): raise PresetFormatError(f"not a {fmt.title()} patch")
        res.requires_base_state = True
        require_base(f"a {fmt.title()} patch")
        if not describe_only: res.state = fn(base_state, data, orig)
        return res
    if fmt == "cherry":
        if not is_cherry_preset(data): raise PresetFormatError("not a Cherry Audio preset")
        if describe_only: res.requires_base_state = True
        else: res.state = cherry_with_preset(base_state, data)
        return res
    if fmt == "fxp":
        if not is_vst2_chunk(data): raise PresetFormatError("not a VST2 .fxp/.fxb file")
        if pp and pp.suffix.lower() == ".fxb":
            progs = bank_programs(pp, data)
            if progs:
                sel = resolve_bank_program(progs, selector) if selector is not None else progs[0]
                res.bank, res.program_index, res.program_name, res.name = orig, sel.index, sel.name, sel.name or orig
                res.state = _decode_fxb_program(data, sel.index)
                return res
            if selector is not None: raise PresetFormatError("this .fxb bank has no addressable programs")
        res.state = fxp_chunk(data)
        return res
    if fmt == "h2p": res.state = data; return res
    if fmt in {"juce-valuetree", "juce-xml", "decentsampler", "soundbox", "juce-string", "raw"}:
        res.state = _decode_standalone(fmt, data, pp or Path("preset"))
        if fmt == "raw": res.requires_base_state = True
        return res
    raise PresetFormatError(f"unknown preset format {fmt!r}")

def describe_preset(path: str | Path, data: bytes | None = None) -> DecodedPreset:
    return _process_preset(path, data, describe_only=True)

def _decode_standalone(fmt: str, data: bytes, p: Path) -> bytes | None:
    if fmt == "juce-valuetree": return value_tree_to_juce_xml(data)
    if fmt == "juce-xml": return juce_xml_state(data, preset_path=p)
    if fmt == "decentsampler": return decent_sampler_state(data, p)
    if fmt == "soundbox": return soundbox_state(data)
    return juce_string_state(data) if fmt == "juce-string" else None

def _decode_fxb_program(data: bytes, idx: int) -> bytes:
    if is_full_bucket_bank(data):
        progs = full_bucket_bank_programs(data)
        if 0 <= idx < len(progs): return progs[idx][1]
        raise PresetFormatError(f"this bank has no program {idx}")
    return obxd_bank_state(data, idx) if obxd_bank_programs(data) else fxp_chunk(data)

def decode_preset(path: str | Path | None = None, data: bytes | None = None, *, format: str = "",
                  selector: str | int | None = None, base_state: bytes | None = None) -> DecodedPreset:
    return _process_preset(path, data, format=format, selector=selector, base_state=base_state, describe_only=False)
