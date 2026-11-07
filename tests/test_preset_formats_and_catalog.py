"""Unit tests for third-party preset decoders and the vendor catalog."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest

from mxm_mcp import preset_catalog, preset_formats, vst3_presets

CID = "A1B2C3D4E5F60718293A4B4C55667788"


# ---------------------------------------------------------------------------
# Synthetic fixture builders
# ---------------------------------------------------------------------------

def _cint(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    negative = value < 0
    raw = -value if negative else value
    payload = bytearray()
    while raw:
        payload.append(raw & 0xFF)
        raw >>= 8
    return bytes([len(payload) | (0x80 if negative else 0)]) + bytes(payload)


def _cstr(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def _var_string(text: str) -> bytes:
    raw = text.encode("utf-8") + b"\x00"
    return _cint(1 + len(raw)) + b"\x05" + raw


def _var_double(value: float) -> bytes:
    return _cint(9) + b"\x04" + struct.pack("<d", value)


def _value_tree_node(name: str, props=(), kids=()) -> bytes:
    out = bytearray(_cstr(name))
    out.extend(_cint(len(props)))
    for key, value in props:
        out.extend(_cstr(key))
        out.extend(value)
    out.extend(_cint(len(kids)))
    for kid in kids:
        out.extend(kid)
    return bytes(out)


def _msgpack(value):
    if value is None:
        return b"\xc0"
    if value is True:
        return b"\xc3"
    if value is False:
        return b"\xc2"
    if isinstance(value, int):
        if 0 <= value < 128:
            return bytes([value])
        if -32 <= value < 0:
            return bytes([value & 0xFF])
        if 0 <= value < 2 ** 32:
            return b"\xce" + struct.pack(">I", value)
        return b"\xd3" + struct.pack(">q", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        if len(raw) < 32:
            return bytes([0xA0 | len(raw)]) + raw
        return b"\xd9" + bytes([len(raw)]) + raw
    if isinstance(value, list):
        return bytes([0x90 | len(value)]) + b"".join(_msgpack(item) for item in value)
    if isinstance(value, dict):
        return bytes([0x80 | len(value)]) + b"".join(
            _msgpack(key) + _msgpack(item) for key, item in value.items())
    raise TypeError(type(value))


def _riff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    return (chunk_id + struct.pack("<I", len(payload)) + payload
            + (b"\x00" if len(payload) % 2 else b""))


def _nks_file(*, state=b"PLUGIN STATE", name="Warm Pad",
              vendor="Example Audio", bankchain=("Massive", "Pads"),
              magic=0x12345678, uid=(0x11, 0x22, 0x33, 0x44)) -> bytes:
    nisi = _msgpack({"name": name, "vendor": vendor,
                     "bankchain": list(bankchain),
                     "types": [["Pad", "Warm"]]})
    plid = _msgpack({"VST.magic": magic, "VST3.uid": list(uid)})
    chunks = (_riff_chunk(b"NISI", b"\x00\x00\x00\x00" + nisi)
              + _riff_chunk(b"PLID", b"\x00\x00\x00\x00" + plid)
              + _riff_chunk(b"PCHK", b"\x01\x00\x00\x00" + state))
    body = b"NIKS" + chunks
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _dx7_cartridge() -> bytes:
    cart = bytearray(4104)
    cart[0] = 0xF0
    cart[1] = 0x43
    cart[3] = 0x09
    cart[4] = 0x20
    cart[5] = 0x00
    for voice in range(32):
        base = 6 + voice * 128
        for index in range(128):
            cart[base + index] = (voice + index) & 0x7F
        cart[base + 118:base + 128] = f"VOICE{voice:02d}".ljust(10).encode("ascii")
    cart[4103] = 0xF7
    return bytes(cart)


def _fxp(program_state: bytes, *, bank: bool = False) -> bytes:
    if bank:
        header = (b"CcnK" + b"\x00\x00\x00\x00" + b"FBCh"
                  + b"\x01\x00\x00\x00" + b"TEST" + b"\x01\x00\x00\x00"
                  + b"\x00\x00\x00\x01" + b"\x00" * 128)
    else:
        header = (b"CcnK" + b"\x00\x00\x00\x00" + b"FPCh"
                  + b"\x01\x00\x00\x00" + b"TEST" + b"\x01\x00\x00\x00"
                  + b"\x00\x00\x00\x01"
                  + b"Program".ljust(28, b"\x00"))
    total = len(header) + 4 + len(program_state)
    header = header[:4] + struct.pack(">I", total - 8) + header[8:]
    return header + struct.pack(">I", len(program_state)) + program_state


def _obxd_bank() -> bytes:
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<discoDSP currentProgram="0">'
           '<program programName="Alpha"/>'
           '<program programName="Beta &amp; Gamma"/>'
           '</discoDSP>')
    chunk = b"VC2!" + struct.pack("<I", len(xml) + 1) + xml.encode() + b"\x00"
    header = (b"CcnK" + b"\x00\x00\x00\x00" + b"FBCh"
              + b"\x01\x00\x00\x00" + b"OBXD" + b"\x01\x00\x00\x00"
              + b"\x00\x00\x00\x02" + b"\x00" * 128)
    total = len(header) + 4 + len(chunk)
    header = header[:4] + struct.pack(">I", total - 8) + header[8:]
    return header + struct.pack(">I", len(chunk)) + chunk


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# NKS
# ---------------------------------------------------------------------------

def test_nks_riff_metadata_and_pchk_state():
    data = _nks_file()
    parsed = preset_formats.parse_nks(data)
    assert parsed.state == b"PLUGIN STATE"
    assert parsed.name == "Warm Pad"
    assert parsed.vendor == "Example Audio"
    assert parsed.bank == "Massive"
    assert parsed.category == "Pad/Warm"
    assert parsed.magic == "12345678"
    assert parsed.uid == "00000011000000220000003300000044"


def test_nks_pchk_without_version_prefix_is_kept_intact():
    state = b"VC2!\x05\x00\x00\x00hello"
    data = _nks_file(state=state)
    assert preset_formats.nks_plugin_chunk(data) == state


def test_nks_without_plugin_chunk_is_rejected():
    body = b"NIKS" + _riff_chunk(b"NISI", b"\x00\x00\x00\x00" + _msgpack({"name": "x"}))
    data = b"RIFF" + struct.pack("<I", len(body)) + body
    with pytest.raises(preset_formats.PresetFormatError):
        preset_formats.nks_plugin_chunk(data)


# ---------------------------------------------------------------------------
# DX7 / Dexed
# ---------------------------------------------------------------------------

def test_dx7_cartridge_names_and_155_byte_unpacking():
    cart = _dx7_cartridge()
    assert preset_formats.is_dx7_cartridge(cart)
    names = preset_formats.dx7_voice_names(cart)
    assert len(names) == 32
    assert names[0] == "VOICE00"
    assert names[14] == "VOICE14"
    voice = preset_formats.unpack_dx7_voice(cart[6 + 14 * 128:6 + 15 * 128])
    assert len(voice) == 155
    assert voice[-10:].rstrip() == b"VOICE14"


def test_dexed_injects_voice_and_cartridge_into_juce_xml_state():
    cart = _dx7_cartridge()
    state = preset_formats.dexed_with_voice(None, cart, 14)
    assert state[:4] == b"VC2!"
    length = int.from_bytes(state[4:8], "little")
    xml = state[8:8 + length].rstrip(b"\x00").decode()
    assert "<dexedState" in xml
    assert 'currentProgram="14"' in xml
    assert 'opSwitch="111111"' in xml
    for attribute in ("base64:sysex", "base64:program"):
        start = xml.find(f'{attribute}="') + len(attribute) + 2
        encoded = xml[start:xml.find('"', start)]
        decoded = preset_formats.decode_juce_base64(encoded)
        assert decoded
        if attribute == "base64:sysex":
            assert decoded == cart
        else:
            assert len(decoded) == 161
            assert decoded[:155] == preset_formats.unpack_dx7_voice(
                cart[6 + 14 * 128:6 + 15 * 128])
            assert decoded[155] == 0x3F


def test_dexed_patches_an_existing_state_without_losing_its_tail():
    cart = _dx7_cartridge()
    template_xml = ('<dexedState currentProgram="0" opSwitch="111111" '
                    'base64:sysex="" base64:program="1.AA"/>')
    template = b"VC2!" + struct.pack("<I", len(template_xml) + 1) + template_xml.encode() + b"\x00"
    state = preset_formats.dexed_with_voice(template, cart, 3)
    xml = state[8:].rstrip(b"\x00").decode()
    assert 'currentProgram="3"' in xml
    assert 'base64:sysex="' in xml


# ---------------------------------------------------------------------------
# JUCE XML / ValueTree
# ---------------------------------------------------------------------------

def test_juce_value_tree_converts_to_binary_xml():
    tree = _value_tree_node(
        "Odin",
        props=[("patchName", _var_string("Glass Pad")), ("gain", _var_double(0.5))],
        kids=[_value_tree_node("oscillator", props=[("type", _var_string("wavetable"))])],
    )
    xml = preset_formats.value_tree_to_xml(tree)
    assert "<Odin" in xml
    assert 'patchName="Glass Pad"' in xml
    assert "<oscillator" in xml
    state = preset_formats.value_tree_to_juce_xml(tree)
    assert state[:4] == b"VC2!"
    length = int.from_bytes(state[4:8], "little")
    assert state[8 + length - 1] == 0
    assert b"<Odin" in state[8:8 + length]


def test_juce_xml_state_upgrades_vaporizer_patch_version_and_wraps_nul():
    xml = '<VASTvaporizer2 PatchVersion="VASTVaporizerParamsV2.00000"/>'
    state = preset_formats.juce_xml_state(xml)
    assert state[:4] == b"VC2!"
    length = int.from_bytes(state[4:8], "little")
    body = state[8:8 + length]
    assert b"V2.10000" in body
    assert body.endswith(b"\x00")


def test_decent_sampler_state_adds_sample_path(tmp_path):
    preset = tmp_path / "library" / "Keys.dspreset"
    data = b'<?xml version="1.0"?><DecentSampler><groups/></DecentSampler>'
    state = preset_formats.decent_sampler_state(data, preset)
    length = int.from_bytes(state[4:8], "little")
    body = state[8:8 + length].decode()
    assert f'_samplePath="{preset.parent}"' in body
    assert "<DecentSampler" in body


def test_cherry_preset_merges_into_saved_state():
    preset = _value_tree_node(
        "main",
        props=[("presetName", _var_string("Brass"))],
        kids=[_value_tree_node("pd", props=[("cutoff", _var_double(0.7))])],
    )
    base = _value_tree_node(
        "savedState",
        kids=[_value_tree_node("curpreset", props=[("displayName", _var_string("Init"))]),
              _value_tree_node("pt", props=[("presetName", _var_string("Init"))])],
    ) + b"TAIL"
    state = preset_formats.cherry_with_preset(base, preset)
    assert state.endswith(b"TAIL")
    merged = preset_formats.read_value_tree(state[:-4])
    assert merged.type == "savedState"
    assert merged.child("pt") is not None
    assert b"Brass" in state
    assert b"cutoff" in state


# ---------------------------------------------------------------------------
# FXP / FXB
# ---------------------------------------------------------------------------

def test_fxp_program_chunk_is_extracted():
    state = preset_formats.fxp_chunk(_fxp(b"OPAQUE PROGRAM"))
    assert state == b"OPAQUE PROGRAM"


def test_fxb_bank_chunk_is_extracted():
    state = preset_formats.fxp_chunk(_fxp(b"OPAQUE BANK", bank=True))
    assert state == b"OPAQUE BANK"


def test_parameter_list_preset_is_rejected():
    data = (b"CcnK" + b"\x00\x00\x00\x00" + b"FxCk" + b"\x00" * 60)
    with pytest.raises(preset_formats.PresetFormatError):
        preset_formats.fxp_chunk(data)


def test_obxd_bank_program_names_and_state_selection():
    bank = _obxd_bank()
    names = preset_formats.obxd_bank_programs(bank)
    assert names == ["Alpha", "Beta & Gamma"]
    state = preset_formats.obxd_bank_state(bank, 1)
    assert b'currentProgram="1"' in state
    assert b"Beta &amp; Gamma" in state


# ---------------------------------------------------------------------------
# Bank addressing
# ---------------------------------------------------------------------------

def test_bank_reference_split_index_and_name(tmp_path):
    cart_path = _write(tmp_path / "cart.syx", _dx7_cartridge())
    path, selector = preset_formats.split_bank_reference(f"{cart_path}#14")
    assert path == str(cart_path)
    assert selector == "14"
    programs = preset_formats.bank_programs(path)
    assert len(programs) == 32
    assert preset_formats.resolve_bank_program(programs, 14).name == "VOICE14"
    assert preset_formats.resolve_bank_program(programs, "voice09").index == 9
    with pytest.raises(preset_formats.PresetFormatError):
        preset_formats.resolve_bank_program(programs, 32)
    with pytest.raises(preset_formats.PresetFormatError):
        preset_formats.resolve_bank_program(programs, "missing")


def test_decode_dx7_bank_program_by_index(tmp_path):
    cart_path = _write(tmp_path / "cart.syx", _dx7_cartridge())
    decoded = preset_formats.decode_preset(cart_path, selector=14)
    assert decoded.program_index == 14
    assert decoded.program_name == "VOICE14"
    assert decoded.state[:4] == b"VC2!"
    assert b'currentProgram="14"' in decoded.state


def test_obxd_bank_programs_are_addressable(tmp_path):
    bank_path = _write(tmp_path / "bank.fxb", _obxd_bank())
    programs = preset_catalog.bank_programs(bank_path)
    assert [program.name for program in programs] == ["Alpha", "Beta & Gamma"]
    decoded = preset_formats.decode_preset(bank_path, selector="Beta & Gamma")
    assert b'currentProgram="1"' in decoded.state


# ---------------------------------------------------------------------------
# Serum 2
# ---------------------------------------------------------------------------

def test_serum_processor_file_is_already_component_state():
    header = json.dumps({"component": "processor", "product": "Serum 2"}).encode()
    data = (b"XferJson\x00" + struct.pack("<Q", len(header)) + header)
    assert preset_formats.is_xfer_json(data)
    assert preset_formats.detect_format(Path("Bass.SerumPreset"), data) == "serum"
    processor, controller = preset_formats.serum_preset_to_states(data)
    assert processor == data
    assert controller is None


@pytest.mark.skipif(preset_formats._zstd_library() is None,
                    reason="libzstd is required for Serum 2 containers")
def test_serum_xfer_container_roundtrips_cbor_payload():
    header = {"component": "preset", "product": "Serum 2", "version": "2.0"}
    payload = {"presetName": "Wide Bass", "presetAuthor": "Ada", "Osc": {"warp": 0.5}}
    data = preset_formats.write_xfer(header, payload)
    parsed_header, parsed_payload = preset_formats.read_xfer(data)
    assert parsed_header["hash"]
    assert parsed_payload == payload
    processor, controller = preset_formats.serum_preset_to_states(data)
    assert processor[:9] == b"XferJson\x00"
    assert controller[:9] == b"XferJson\x00"
    _, processor_payload = preset_formats.read_xfer(processor)
    _, controller_payload = preset_formats.read_xfer(controller)
    assert processor_payload["component"] == "processor"
    assert "Osc" not in processor_payload
    assert controller_payload["component"] == "controller"
    assert controller_payload["presetName"] == "Wide Bass"


# ---------------------------------------------------------------------------
# Vendor directory catalog
# ---------------------------------------------------------------------------

def test_surge_vital_and_uhe_candidate_directories(tmp_path, monkeypatch):
    home = tmp_path / "home"
    environ = {"HOME": str(home), "PROGRAMDATA": str(tmp_path / "programdata"),
               "APPDATA": str(tmp_path / "appdata")}
    surge = preset_catalog.vendor_preset_dirs(
        {"name": "Surge XT", "vendor": "Surge Synth Team"},
        platform="linux", environ=environ, home=home)
    assert Path("/usr/share/surge-xt/patches_factory") in surge
    assert home / "Documents/Surge XT/Patches" in surge

    effects = preset_catalog.vendor_preset_dirs(
        {"name": "Surge XT Effects", "vendor": "Surge Synth Team"},
        platform="win32", environ=environ, home=home)
    assert tmp_path / "programdata/Surge XT/fx_presets" in effects

    vital = preset_catalog.vendor_preset_dirs(
        {"name": "Vital", "vendor": "Vital Audio"},
        platform="linux", environ=environ, home=home)
    assert home / "Music/Vital" in vital
    assert home / "Documents/Vital" in vital

    zebra = preset_catalog.vendor_preset_dirs(
        {"name": "Zebralette3", "vendor": "u-he"},
        platform="linux", environ=environ, home=home)
    assert home / ".u-he/Zebralette3/Presets" in zebra
    assert home / "Documents/u-he/Zebralette3.data/Presets" in zebra


def test_dexed_cartridge_directories_per_platform(tmp_path):
    home = tmp_path / "home"
    environ = {"HOME": str(home), "APPDATA": str(tmp_path / "appdata"),
               "XDG_DATA_HOME": str(tmp_path / "xdg")}
    linux = preset_catalog.vendor_preset_dirs(
        {"name": "Dexed", "vendor": "Digital Suburban"},
        platform="linux", environ=environ, home=home)
    assert tmp_path / "xdg/DigitalSuburban/Dexed/Cartridges" in linux
    windows = preset_catalog.vendor_preset_dirs(
        {"name": "Dexed", "vendor": "Digital Suburban"},
        platform="win32", environ=environ, home=home)
    assert tmp_path / "appdata/DigitalSuburban/Dexed/Cartridges" in windows


def test_decent_sampler_reads_its_settings_directory(tmp_path):
    home = tmp_path / "home"
    settings = home / ".config/Decidedly/DecentSampler/DecentSampler.xml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '<settings><entry name="sampleLibraryDirectory" val="' +
        str(tmp_path / "Libraries") + '"/></settings>')
    libraries = preset_catalog.decent_sampler_libraries(
        platform="linux", environ={"HOME": str(home)}, home=home)
    assert libraries == [tmp_path / "Libraries"]
    dirs = preset_catalog.vendor_preset_dirs(
        {"name": "Decent Sampler", "vendor": "Decidedly"},
        platform="linux", environ={"HOME": str(home)}, home=home)
    assert tmp_path / "Libraries" in dirs


def test_catalog_expands_dx7_cartridge_into_32_addressable_voices(tmp_path):
    cart_path = _write(tmp_path / "cartridges/cart.syx", _dx7_cartridge())
    entries = preset_catalog.cataloged_entries(
        {"name": "Dexed", "vendor": "Digital Suburban", "module": "/plugins/Dexed.vst3",
         "cid": CID, "is_instrument": True},
        dirs=[cart_path.parent])
    assert len(entries) == 32
    assert entries[14].source == f"{cart_path}#14"
    assert entries[14].name == "VOICE14"
    assert all(entry.format == "dx7" for entry in entries)


# ---------------------------------------------------------------------------
# Integration with vst3_presets
# ---------------------------------------------------------------------------

def _plugin(bundle: Path) -> dict:
    return {"name": "Dexed", "vendor": "Digital Suburban", "module": str(bundle),
            "cid": CID, "is_instrument": True}


def test_discovery_indexes_dx7_bank_and_loads_selected_voice(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path / "Dexed.vst3")
    cart_path = _write(tmp_path / "cartridges/cart.syx", _dx7_cartridge())
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None, **kwargs: [cart_path.parent])
    index = vst3_presets.discover_vst3_presets([plugin], plugin_scoped=True, refresh=True)
    assert len(index["presets"]) == 32
    selected = next(item for item in index["presets"] if item["name"] == "VOICE14")
    assert selected["source"] == f"{cart_path}#14"
    assert selected["plugin_cid"] == CID
    assert selected["loadable"] is True
    state = vst3_presets.load_vst3preset_state(selected, index["search_paths"])
    assert state is not None
    decoded = base64.b64decode(state)
    assert decoded[:4] == b"VC2!"
    assert b'currentProgram="14"' in decoded


def test_discovery_indexes_nks_preset_for_matching_magic(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path / "Synth.vst3")
    plugin = {**plugin, "name": "Mystery Synth", "cid": "12345678" + "0" * 24}
    nks_path = _write(tmp_path / "nks/Warm Pad.nksf", _nks_file())
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None, **kwargs: [nks_path.parent])
    index = vst3_presets.discover_vst3_presets([plugin], plugin_scoped=True, refresh=True)
    preset, = index["presets"]
    assert preset["name"] == "Warm Pad"
    assert preset["format"] == "nksf"
    assert preset["plugin_cid"] == plugin["cid"]
    state = vst3_presets.load_vst3preset_state(preset, index["search_paths"])
    assert base64.b64decode(state) == b"PLUGIN STATE"


def test_discovery_indexes_obxd_bank_programs(tmp_path, monkeypatch):
    plugin = {**_plugin(tmp_path / "OB-Xd.vst3"), "name": "OB-Xd",
              "vendor": "discoDSP"}
    bank_path = _write(tmp_path / "banks/bank.fxb", _obxd_bank())
    monkeypatch.setattr(vst3_presets, "preset_search_paths",
                        lambda plugins=None, **kwargs: [bank_path.parent])
    index = vst3_presets.discover_vst3_presets([plugin], plugin_scoped=True, refresh=True)
    assert [preset["name"] for preset in index["presets"]] == ["Alpha", "Beta & Gamma"]
    selected = index["presets"][1]
    state = vst3_presets.load_vst3preset_state(selected, index["search_paths"])
    assert b'currentProgram="1"' in base64.b64decode(state)
