"""Unit tests for third-party preset decoders and the vendor catalog."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest

from mxm_mcp import preset_catalog, preset_formats, vst3_presets

CID = "A1B2C3D4E5F60718293A4B4C55667788"


def _cint(val: int) -> bytes:
    if val == 0: return b"\x00"
    neg, raw, buf = val < 0, (-val if val < 0 else val), bytearray()
    while raw: buf.append(raw & 0xFF); raw >>= 8
    return bytes([len(buf) | (0x80 if neg else 0)]) + bytes(buf)


def _var_string(text: str) -> bytes:
    raw = text.encode("utf-8") + b"\x00"
    return _cint(1 + len(raw)) + b"\x05" + raw


def _var_double(value: float) -> bytes:
    return _cint(9) + b"\x04" + struct.pack("<d", value)


def _value_tree_node(name: str, props=(), kids=()) -> bytes:
    out = bytearray(name.encode("utf-8") + b"\x00" + _cint(len(props)))
    for k, v in props: out.extend(k.encode("utf-8") + b"\x00" + v)
    out.extend(_cint(len(kids)))
    for k in kids: out.extend(k)
    return bytes(out)


def _msgpack(v):
    if v is None: return b"\xc0"
    if isinstance(v, bool): return b"\xc3" if v else b"\xc2"
    if isinstance(v, int): return bytes([v]) if 0 <= v < 128 else (bytes([v & 0xFF]) if -32 <= v < 0 else b"\xce" + struct.pack(">I", v))
    if isinstance(v, str):
        raw = v.encode("utf-8")
        return (bytes([0xA0 | len(raw)]) if len(raw) < 32 else b"\xd9" + bytes([len(raw)])) + raw
    if isinstance(v, list): return bytes([0x90 | len(v)]) + b"".join(_msgpack(i) for i in v)
    if isinstance(v, dict): return bytes([0x80 | len(v)]) + b"".join(_msgpack(k) + _msgpack(val) for k, val in v.items())
    raise TypeError(type(v))


def _riff_chunk(cid: bytes, payload: bytes) -> bytes:
    return cid + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) % 2 else b"")


def _nks_file(*, state=b"PLUGIN STATE", name="Warm Pad", vendor="Example Audio", bankchain=("Massive", "Pads"), magic=0x12345678, uid=(0x11, 0x22, 0x33, 0x44)) -> bytes:
    nisi = _msgpack({"name": name, "vendor": vendor, "bankchain": list(bankchain), "types": [["Pad", "Warm"]]})
    plid = _msgpack({"VST.magic": magic, "VST3.uid": list(uid)})
    body = b"NIKS" + _riff_chunk(b"NISI", b"\x00" * 4 + nisi) + _riff_chunk(b"PLID", b"\x00" * 4 + plid) + _riff_chunk(b"PCHK", b"\x01\x00\x00\x00" + state)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _dx7_cartridge() -> bytes:
    cart = bytearray(4104)
    cart[0], cart[1], cart[3], cart[4], cart[5], cart[4103] = 0xF0, 0x43, 0x09, 0x20, 0x00, 0xF7
    for v in range(32):
        base = 6 + v * 128
        for i in range(128): cart[base + i] = (v + i) & 0x7F
        cart[base + 118:base + 128] = f"VOICE{v:02d}".ljust(10).encode("ascii")
    return bytes(cart)


def _fxp(program_state: bytes, *, bank: bool = False) -> bytes:
    magic = b"FBCh" if bank else b"FPCh"
    header = b"CcnK\x00\x00\x00\x00" + magic + b"\x01\x00\x00\x00TEST\x01\x00\x00\x00\x00\x00\x00\x01" + (b"\x00" * 128 if bank else b"Program".ljust(28, b"\x00"))
    return header[:4] + struct.pack(">I", len(header) + len(program_state) - 4) + header[8:] + struct.pack(">I", len(program_state)) + program_state


def _obxd_bank() -> bytes:
    xml = '<?xml version="1.0" encoding="UTF-8"?><discoDSP currentProgram="0"><program programName="Alpha"/><program programName="Beta &amp; Gamma"/></discoDSP>'
    chunk = b"VC2!" + struct.pack("<I", len(xml) + 1) + xml.encode() + b"\x00"
    header = b"CcnK\x00\x00\x00\x00FBCh\x01\x00\x00\x00OBXD\x01\x00\x00\x00\x00\x00\x00\x02" + b"\x00" * 128
    return header[:4] + struct.pack(">I", len(header) + len(chunk) - 4) + header[8:] + struct.pack(">I", len(chunk)) + chunk


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data); return path


# ---- tests ----

def test_nks_riff_metadata_and_pchk_state():
    parsed = preset_formats.parse_nks(_nks_file())
    assert parsed.state == b"PLUGIN STATE" and parsed.name == "Warm Pad" and parsed.vendor == "Example Audio"
    assert parsed.bank == "Massive" and parsed.category == "Pad/Warm" and parsed.magic == "12345678"
    assert parsed.uid == "00000011000000220000003300000044"


def test_nks_pchk_without_version_prefix_is_kept_intact():
    state = b"VC2!\x05\x00\x00\x00hello"
    assert preset_formats.nks_plugin_chunk(_nks_file(state=state)) == state


def test_nks_without_plugin_chunk_is_rejected():
    body = b"NIKS" + _riff_chunk(b"NISI", b"\x00\x00\x00\x00" + _msgpack({"name": "x"}))
    with pytest.raises(preset_formats.PresetFormatError):
        preset_formats.nks_plugin_chunk(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_dx7_cartridge_names_and_155_byte_unpacking():
    cart = _dx7_cartridge()
    assert preset_formats.is_dx7_cartridge(cart)
    names = preset_formats.dx7_voice_names(cart)
    assert len(names) == 32 and names[0] == "VOICE00" and names[14] == "VOICE14"
    voice = preset_formats.unpack_dx7_voice(cart[6 + 14 * 128:6 + 15 * 128])
    assert len(voice) == 155 and voice[-10:].rstrip() == b"VOICE14"


def test_dexed_injects_voice_and_cartridge_into_juce_xml_state():
    cart = _dx7_cartridge()
    state = preset_formats.dexed_with_voice(None, cart, 14)
    assert state[:4] == b"VC2!"
    xml = state[8:8 + int.from_bytes(state[4:8], "little")].rstrip(b"\x00").decode()
    assert "<dexedState" in xml and 'currentProgram="14"' in xml and 'opSwitch="111111"' in xml
    for attr in ("base64:sysex", "base64:program"):
        s = xml.find(f'{attr}="') + len(attr) + 2
        dec = preset_formats.decode_juce_base64(xml[s:xml.find('"', s)])
        assert dec == (cart if attr == "base64:sysex" else dec)
        if attr == "base64:program":
            assert len(dec) == 161 and dec[155] == 0x3F


def test_dexed_patches_an_existing_state_without_losing_its_tail():
    cart = _dx7_cartridge()
    tmpl = b"VC2!\x50\x00\x00\x00" + b'<dexedState currentProgram="0" opSwitch="111111" base64:sysex="" base64:program="1.AA"/>\x00'
    state = preset_formats.dexed_with_voice(tmpl, cart, 3)
    xml = state[8:].rstrip(b"\x00").decode()
    assert 'currentProgram="3"' in xml and 'base64:sysex="' in xml


def test_juce_value_tree_converts_to_binary_xml():
    tree = _value_tree_node("Odin", props=[("patchName", _var_string("Glass Pad")), ("gain", _var_double(0.5))],
                            kids=[_value_tree_node("oscillator", props=[("type", _var_string("wavetable"))])])
    xml = preset_formats.value_tree_to_xml(tree)
    assert "<Odin" in xml and 'patchName="Glass Pad"' in xml and "<oscillator" in xml
    state = preset_formats.value_tree_to_juce_xml(tree)
    assert state[:4] == b"VC2!" and b"<Odin" in state


def test_juce_xml_state_upgrades_vaporizer_patch_version_and_wraps_nul():
    state = preset_formats.juce_xml_state('<VASTvaporizer2 PatchVersion="VASTVaporizerParamsV2.00000"/>')
    assert state[:4] == b"VC2!" and b"V2.10000" in state and state.endswith(b"\x00")


def test_decent_sampler_state_adds_sample_path(tmp_path):
    preset = tmp_path / "library/Keys.dspreset"
    state = preset_formats.decent_sampler_state(b'<?xml version="1.0"?><DecentSampler><groups/></DecentSampler>', preset)
    assert f'_samplePath="{preset.parent}"' in state.decode(errors="ignore")


def test_cherry_preset_merges_into_saved_state():
    preset = _value_tree_node("main", props=[("presetName", _var_string("Brass"))],
                              kids=[_value_tree_node("pd", props=[("cutoff", _var_double(0.7))])])
    base = _value_tree_node("savedState", kids=[
        _value_tree_node("curpreset", props=[("displayName", _var_string("Init"))]),
        _value_tree_node("pt", props=[("presetName", _var_string("Init"))]),
    ]) + b"TAIL"
    state = preset_formats.cherry_with_preset(base, preset)
    assert state.endswith(b"TAIL")
    merged = preset_formats.read_value_tree(state[:-4])
    assert merged.type == "savedState" and merged.child("pt") is not None


def test_fxp_program_chunk_is_extracted():
    assert preset_formats.fxp_chunk(_fxp(b"OPAQUE PROGRAM")) == b"OPAQUE PROGRAM"
    assert preset_formats.fxp_chunk(_fxp(b"OPAQUE BANK", bank=True)) == b"OPAQUE BANK"
    with pytest.raises(preset_formats.PresetFormatError):
        preset_formats.fxp_chunk(b"CcnK\x00\x00\x00\x00FxCk" + b"\x00" * 60)


def test_obxd_bank_program_names_and_state_selection():
    bank = _obxd_bank()
    assert preset_formats.obxd_bank_programs(bank) == ["Alpha", "Beta & Gamma"]
    state = preset_formats.obxd_bank_state(bank, 1)
    assert b'currentProgram="1"' in state and b"Beta &amp; Gamma" in state


def test_bank_reference_split_index_and_name(tmp_path):
    cart_path = _write(tmp_path / "cart.syx", _dx7_cartridge())
    path, sel = preset_formats.split_bank_reference(f"{cart_path}#14")
    assert path == str(cart_path) and sel == "14"
    progs = preset_formats.bank_programs(path)
    assert len(progs) == 32 and preset_formats.resolve_bank_program(progs, 14).name == "VOICE14"
    assert preset_formats.resolve_bank_program(progs, "voice09").index == 9
    with pytest.raises(preset_formats.PresetFormatError):
        preset_formats.resolve_bank_program(progs, 32)


def test_decode_dx7_bank_program_by_index(tmp_path):
    cart_path = _write(tmp_path / "cart.syx", _dx7_cartridge())
    decoded = preset_formats.decode_preset(cart_path, selector=14)
    assert decoded.program_index == 14 and decoded.program_name == "VOICE14"
    assert decoded.state[:4] == b"VC2!" and b'currentProgram="14"' in decoded.state


def test_obxd_bank_programs_are_addressable(tmp_path):
    bank_path = _write(tmp_path / "bank.fxb", _obxd_bank())
    assert [p.name for p in preset_catalog.bank_programs(bank_path)] == ["Alpha", "Beta & Gamma"]
    decoded = preset_formats.decode_preset(bank_path, selector="Beta & Gamma")
    assert b'currentProgram="1"' in decoded.state


def test_serum_processor_file_is_already_component_state():
    header = json.dumps({"component": "processor", "product": "Serum 2"}).encode()
    data = b"XferJson\x00" + struct.pack("<Q", len(header)) + header
    assert preset_formats.is_xfer_json(data)
    assert preset_formats.detect_format(Path("Bass.SerumPreset"), data) == "serum"
    proc, ctl = preset_formats.serum_preset_to_states(data)
    assert proc == data and ctl is None


@pytest.mark.skipif(preset_formats._zstd_library() is None, reason="libzstd is required for Serum 2 containers")
def test_serum_xfer_container_roundtrips_cbor_payload():
    header = {"component": "preset", "product": "Serum 2", "version": "2.0"}
    payload = {"presetName": "Wide Bass", "presetAuthor": "Ada", "Osc": {"warp": 0.5}}
    data = preset_formats.write_xfer(header, payload)
    parsed_header, parsed_payload = preset_formats.read_xfer(data)
    assert parsed_header["hash"] and parsed_payload == payload
    proc, ctl = preset_formats.serum_preset_to_states(data)
    assert proc[:9] == b"XferJson\x00" and ctl[:9] == b"XferJson\x00"
    _, p_payload = preset_formats.read_xfer(proc)
    _, c_payload = preset_formats.read_xfer(ctl)
    assert p_payload["component"] == "processor" and "Osc" not in p_payload
    assert c_payload["component"] == "controller" and c_payload["presetName"] == "Wide Bass"


def test_surge_vital_and_uhe_candidate_directories(tmp_path):
    home = tmp_path / "home"
    env = {"HOME": str(home), "PROGRAMDATA": str(tmp_path / "programdata"), "APPDATA": str(tmp_path / "appdata")}
    surge = preset_catalog.vendor_preset_dirs({"name": "Surge XT", "vendor": "Surge Synth Team"}, platform="linux", environ=env, home=home)
    assert Path("/usr/share/surge-xt/patches_factory") in surge and home / "Documents/Surge XT/Patches" in surge
    effects = preset_catalog.vendor_preset_dirs({"name": "Surge XT Effects", "vendor": "Surge Synth Team"}, platform="win32", environ=env, home=home)
    assert tmp_path / "programdata/Surge XT/fx_presets" in effects
    vital = preset_catalog.vendor_preset_dirs({"name": "Vital", "vendor": "Vital Audio"}, platform="linux", environ=env, home=home)
    assert home / "Music/Vital" in vital and home / "Documents/Vital" in vital
    zebra = preset_catalog.vendor_preset_dirs({"name": "Zebralette3", "vendor": "u-he"}, platform="linux", environ=env, home=home)
    assert home / ".u-he/Zebralette3/Presets" in zebra


def test_dexed_cartridge_directories_per_platform(tmp_path):
    home = tmp_path / "home"
    env = {"HOME": str(home), "APPDATA": str(tmp_path / "appdata"), "XDG_DATA_HOME": str(tmp_path / "xdg")}
    assert tmp_path / "xdg/DigitalSuburban/Dexed/Cartridges" in preset_catalog.vendor_preset_dirs({"name": "Dexed", "vendor": "Digital Suburban"}, platform="linux", environ=env, home=home)
    assert tmp_path / "appdata/DigitalSuburban/Dexed/Cartridges" in preset_catalog.vendor_preset_dirs({"name": "Dexed", "vendor": "Digital Suburban"}, platform="win32", environ=env, home=home)


def test_decent_sampler_reads_its_settings_directory(tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config/Decidedly/DecentSampler/DecentSampler.xml"; cfg.parent.mkdir(parents=True)
    cfg.write_text(f'<settings><entry name="sampleLibraryDirectory" val="{tmp_path / "Libraries"}"/></settings>')
    assert preset_catalog.decent_sampler_libraries(platform="linux", environ={"HOME": str(home)}, home=home) == [tmp_path / "Libraries"]


def test_catalog_expands_dx7_cartridge_into_32_addressable_voices(tmp_path):
    cart_path = _write(tmp_path / "cartridges/cart.syx", _dx7_cartridge())
    entries = preset_catalog.cataloged_entries({"name": "Dexed", "vendor": "Digital Suburban", "module": "/plugins/Dexed.vst3", "cid": CID, "is_instrument": True}, dirs=[cart_path.parent])
    assert len(entries) == 32 and entries[14].source == f"{cart_path}#14" and entries[14].name == "VOICE14"


def _plugin(bundle: Path) -> dict:
    return {"name": "Dexed", "vendor": "Digital Suburban", "module": str(bundle), "cid": CID, "is_instrument": True}


def test_discovery_indexes_dx7_bank_and_loads_selected_voice(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path / "Dexed.vst3")
    cart_path = _write(tmp_path / "cartridges/cart.syx", _dx7_cartridge())
    monkeypatch.setattr(vst3_presets, "preset_search_paths", lambda *a, **kw: [cart_path.parent])
    index = vst3_presets.discover_vst3_presets([plugin], plugin_scoped=True, refresh=True)
    assert len(index["presets"]) == 32
    selected = next(i for i in index["presets"] if i["name"] == "VOICE14")
    assert selected["source"] == f"{cart_path}#14" and selected["loadable"] is True
    assert b'currentProgram="14"' in base64.b64decode(vst3_presets.load_vst3preset_state(selected, index["search_paths"]))


def test_discovery_indexes_nks_preset_for_matching_magic(tmp_path, monkeypatch):
    plugin = {**_plugin(tmp_path / "Synth.vst3"), "name": "Mystery Synth", "cid": "12345678" + "0" * 24}
    nks_path = _write(tmp_path / "nks/Warm Pad.nksf", _nks_file())
    monkeypatch.setattr(vst3_presets, "preset_search_paths", lambda *a, **kw: [nks_path.parent])
    index = vst3_presets.discover_vst3_presets([plugin], plugin_scoped=True, refresh=True)
    preset = index["presets"][0]
    assert preset["name"] == "Warm Pad" and preset["format"] == "nksf"
    assert base64.b64decode(vst3_presets.load_vst3preset_state(preset, index["search_paths"])) == b"PLUGIN STATE"


def test_discovery_indexes_obxd_bank_programs(tmp_path, monkeypatch):
    plugin = {**_plugin(tmp_path / "OB-Xd.vst3"), "name": "OB-Xd", "vendor": "discoDSP"}
    bank_path = _write(tmp_path / "banks/bank.fxb", _obxd_bank())
    monkeypatch.setattr(vst3_presets, "preset_search_paths", lambda *a, **kw: [bank_path.parent])
    index = vst3_presets.discover_vst3_presets([plugin], plugin_scoped=True, refresh=True)
    assert [p["name"] for p in index["presets"]] == ["Alpha", "Beta & Gamma"]
    assert b'currentProgram="1"' in base64.b64decode(vst3_presets.load_vst3preset_state(index["presets"][1], index["search_paths"]))
