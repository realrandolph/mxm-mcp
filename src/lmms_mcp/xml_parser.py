"""XML parser for LMMS .mmpz and .mmp project files."""

import base64
import struct
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET


def _q_compress(data: bytes) -> bytes:
    """Replicate Qt's qCompress: 4-byte big-endian size header + zlib data."""
    compressed = zlib.compress(data, 9)
    size = len(data)
    return struct.pack(">I", size) + compressed


def _q_uncompress(data: bytes) -> bytes:
    """Replicate Qt's qUncompress: read 4-byte big-endian size header + zlib data."""
    if len(data) < 4:
        raise ValueError("Data too short for qCompress header")
    size = struct.unpack(">I", data[:4])[0]
    decompressed = zlib.decompress(data[4:])
    return decompressed


def _to_int(value, default: int = 0) -> int:
    """Parse an integer attribute value robustly.

    Tolerates float strings like "3072.0" (which Qt's toInt() rejects,
    silently falling back to defaults and breaking clips).
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def load_project(path: str | Path) -> ET.Element:
    """Load an LMMS project file (.mmpz or .mmp) and return the root element.

    .mmpz files use Qt's qCompress format: 4-byte big-endian length header + zlib.
    We try plain XML first, then qUncompress, then raw zlib.
    """
    path = Path(path)
    raw = path.read_bytes()

    # Try plain XML first (.mmp files)
    try:
        root = ET.fromstring(raw)
        return root
    except ET.ParseError:
        pass

    # Try Qt's qCompress format (4-byte header + zlib)
    try:
        decompressed = _q_uncompress(raw)
        root = ET.fromstring(decompressed)
        return root
    except (struct.error, zlib.error, ET.ParseError):
        pass

    # Try raw zlib (no header)
    try:
        decompressed = zlib.decompress(raw)
        root = ET.fromstring(decompressed)
        return root
    except (zlib.error, ET.ParseError):
        pass

    raise ValueError(f"Cannot parse {path}: not a valid LMMS project file")


def save_project(path: str | Path, root: ET.Element, compressed: bool = True) -> None:
    """Save an LMMS project element to a file.

    If compressed is True, writes .mmpz (Qt qCompress format).
    Otherwise writes plain .mmp XML.
    """
    path = Path(path)
    ET.indent(root, space="  ")
    xml_str = ET.tostring(root, encoding="unicode", xml_declaration=False)
    xml_str = '<?xml version="1.0"?>\n<!DOCTYPE lmms-project>\n' + xml_str

    if compressed:
        data = _q_compress(xml_str.encode("utf-8"))
    else:
        data = xml_str.encode("utf-8")

    path.write_bytes(data)


def create_empty_project(
    bpm: int = 140,
    time_sig_numerator: int = 4,
    time_sig_denominator: int = 4,
    master_volume: int = 100,
    master_pitch: int = 0,
) -> ET.Element:
    """Create a minimal empty LMMS project element tree."""
    from .lmms_app import get_lmms_version

    creator_version = get_lmms_version() or "1.2.0"
    root = ET.Element("lmms-project", {
        "version": "31",
        "creator": "LMMS",
        "creatorplatform": "linux",
        "creatorversion": creator_version,
        "type": "song",
        "creatorplatformtype": "linuxmint",
    })

    head = ET.SubElement(root, "head", {
        "timesig_numerator": str(time_sig_numerator),
        "timesig_denominator": str(time_sig_denominator),
        "bpm": str(bpm),
        "mastervol": str(master_volume),
        "masterpitch": str(master_pitch),
    })

    song = ET.SubElement(root, "song")

    trackcontainer = ET.SubElement(song, "trackcontainer", {
        "type": "song",
        "visible": "1",
        "width": "600",
        "height": "300",
        "x": "5",
        "y": "5",
        "maximized": "0",
        "minimized": "0",
    })

    mixer = ET.SubElement(song, "mixer", {
        "visible": "1",
        "width": "561",
        "height": "332",
        "x": "5",
        "y": "310",
        "maximized": "0",
        "minimized": "0",
    })
    ET.SubElement(mixer, "mixerchannel", {
        "num": "0",
        "muted": "0",
        "volume": "1",
        "name": "Master",
        "soloed": "0",
    }).append(ET.Element("fxchain", {"numofeffects": "0", "enabled": "0"}))

    ET.SubElement(song, "ControllerRackView", {
        "visible": "1", "width": "258", "height": "142",
        "x": "836", "y": "407", "maximized": "0", "minimized": "0",
    })
    ET.SubElement(song, "pianoroll", {
        "visible": "0", "width": "640", "height": "480",
        "x": "1", "y": "1", "maximized": "0", "minimized": "0",
    })
    ET.SubElement(song, "automationeditor", {
        "visible": "0", "width": "640", "height": "400",
        "x": "56", "y": "255", "maximized": "0", "minimized": "0",
    })
    ET.SubElement(song, "projectnotes", {
        "visible": "0", "width": "640", "height": "400",
        "x": "1", "y": "1", "maximized": "0", "minimized": "0",
    })
    ET.SubElement(song, "timeline", {
        "lp0pos": "0", "lp1pos": "192", "lpstate": "0",
    })
    ET.SubElement(song, "controllers")

    # LMMS indexes these tables while loading version-31 projects. Keeping
    # the default tables present matches projects written by LMMS itself.
    scales = ET.SubElement(song, "scales")
    default_scale = ET.SubElement(scales, "scale", {"description": "12-tone EDO (default)"})
    ET.SubElement(default_scale, "interval", {"num": "1", "den": "1"})
    for cents in range(100, 1200, 100):
        ET.SubElement(default_scale, "interval", {"cents": str(cents)})
    ET.SubElement(default_scale, "interval", {"num": "2", "den": "1"})
    for _ in range(11):
        scale = ET.SubElement(scales, "scale", {"description": "empty"})
        ET.SubElement(scale, "interval", {"num": "1", "den": "1"})

    keymaps = ET.SubElement(song, "keymaps")
    ET.SubElement(keymaps, "keymap", {
        "last_key": "127", "description": "all keys 1:1 (default)",
        "base_freq": "440", "first_key": "0", "base_key": "69", "middle_key": "0",
    })
    for _ in range(9):
        ET.SubElement(keymaps, "keymap", {
            "last_key": "127", "description": "empty", "base_freq": "440",
            "first_key": "0", "base_key": "69", "middle_key": "60",
        })

    return root


def element_to_dict(elem: ET.Element) -> dict:
    """Recursively convert an XML element to a dictionary."""
    result = dict(elem.attrib)

    children = list(elem)
    if children:
        child_dict: dict[str, list] = {}
        for child in children:
            tag = child.tag
            if tag not in child_dict:
                child_dict[tag] = []
            child_dict[tag].append(element_to_dict(child))
        result["_children"] = child_dict

    if elem.text and elem.text.strip():
        result["_text"] = elem.text.strip()

    return result


def find_tracks(root: ET.Element) -> list[ET.Element]:
    """Find all <track> elements in the project."""
    song = root.find("song")
    if song is None:
        return []

    container = song.find("trackcontainer[@type='song']")
    if container is None:
        return []

    return container.findall("track")


def find_mixer_channels(root: ET.Element) -> list[ET.Element]:
    """Find all <mixerchannel> elements."""
    song = root.find("song")
    if song is None:
        return []

    mixer = song.find("mixer")
    if mixer is None:
        return []

    return mixer.findall("mixerchannel")


def get_track_type(elem: ET.Element) -> int:
    """Get the track type integer from a <track> element."""
    return int(elem.get("type", "0"))


def get_track_name(elem: ET.Element) -> str:
    """Get the track name from a <track> element."""
    return elem.get("name", "Unnamed")


TRACK_TYPE_NAMES = {
    0: "Instrument",
    1: "Pattern",
    2: "Sample",
    3: "Event",
    4: "Video",
    5: "Automation",
    6: "HiddenAutomation",
}


def track_type_name(type_id: int) -> str:
    """Convert a track type integer to its name."""
    return TRACK_TYPE_NAMES.get(type_id, f"Unknown({type_id})")


def _make_instrument_track(
    parent: ET.Element,
    name: str,
    instrument: str = "tripleoscillator",
    mixer_channel: int = 0,
    volume: int = 100,
    panning: int = 0,
) -> ET.Element:
    """Append an instrument <track> element to a trackcontainer parent."""
    track = ET.SubElement(parent, "track", {
        "muted": "0",
        "type": "0",
        "name": name,
        "mutedBeforeSolo": "0",
        "solo": "0",
    })

    inst_track = ET.SubElement(track, "instrumenttrack", {
        "enabled": "0",
        "scale": "0",
        "pan": str(panning),
        "mixch": str(mixer_channel),
        "basenote": "69",
        "usemasterpitch": "1",
        "range_import": "1",
        "lastkey": "127",
        "pitchrange": "1",
        "firstkey": "0",
        "pitch": "0",
        "keymap": "0",
        "enablecc": "0",
        "vol": str(volume),
    })

    ET.SubElement(inst_track, "midicontrollers", {
        f"cc{controller}": "0" for controller in range(128)
    })

    inst = ET.SubElement(inst_track, "instrument", {"name": instrument})
    plugin_tag = instrument.replace(" ", "").lower()
    plugin = ET.SubElement(inst, plugin_tag)
    if plugin_tag == "tripleoscillator":
        # These are the defaults written by LMMS 1.3 for a new TripleOscillator.
        plugin.attrib.update({
            "stphdetun1": "0", "finer1": "0", "modalgo3": "2",
            "finel1": "0", "wavetype0": "0", "coarse0": "0",
            "vol0": "33", "vol1": "33", "finer0": "0", "vol2": "33",
            "coarse2": "-24", "coarse1": "-12", "phoffset1": "0",
            "finer2": "0", "finel2": "0", "modalgo1": "2",
            "modalgo2": "2", "wavetype1": "0", "stphdetun0": "0",
            "userwavefile1": "", "useWaveTable2": "1", "phoffset2": "0",
            "pan0": "0", "wavetype2": "0", "userwavefile2": "",
            "phoffset0": "0", "stphdetun2": "0", "useWaveTable3": "1",
            "pan1": "0", "finel0": "0", "useWaveTable1": "1",
            "userwavefile0": "", "pan2": "0",
        })
    ET.SubElement(plugin, "key")

    eldata = ET.SubElement(inst_track, "eldata", {
        "fres": "0.5", "ftype": "0", "fcut": "14000", "fwet": "0",
    })
    ET.SubElement(eldata, "elvol", {
        "lspd_denominator": "4", "dec": "0.5", "userwavefile": "",
        "pdel": "0", "amt": "0", "lspd": "0.1", "ctlenvamt": "0",
        "lspd_syncmode": "0", "att": "0", "lspd_numerator": "4",
        "lshp": "0", "latt": "0", "lpdel": "0", "x100": "0",
        "hold": "0.5", "rel": "0.1", "sustain": "0.5", "lamt": "0",
    })
    for envelope in ("elcut", "elres"):
        ET.SubElement(eldata, envelope, dict(eldata.find("elvol").attrib))

    ET.SubElement(inst_track, "chordcreator", {
        "chord": "0", "chordrange": "1", "chord-enabled": "0",
    })
    ET.SubElement(inst_track, "arpeggiator", {
        "arpskip": "0", "arpgate": "100", "arpmiss": "0",
        "arprange": "1", "arptime_denominator": "4", "arprepeats": "1",
        "arp": "0", "arpmode": "0", "arp-enabled": "0", "arpdir": "0",
        "arpcycle": "0", "arptime_syncmode": "0", "arptime_numerator": "4",
        "arptime": "100",
    })
    ET.SubElement(inst_track, "midiport", {
        "inputcontroller": "0", "fixedoutputvelocity": "-1",
        "inputchannel": "0", "outputcontroller": "0", "writable": "0",
        "outputchannel": "1", "fixedinputvelocity": "-1",
        "fixedoutputnote": "-1", "outputprogram": "1",
        "basevelocity": "63", "readable": "0",
    })
    ET.SubElement(inst_track, "fxchain", {
        "numofeffects": "0", "enabled": "0",
    })

    return track


def add_instrument_track(
    root: ET.Element,
    name: str,
    instrument: str = "tripleoscillator",
    mixer_channel: int = 0,
    volume: int = 100,
    panning: int = 0,
) -> ET.Element:
    """Add an instrument track to the project and return the track element."""
    song = root.find("song")
    container = song.find("trackcontainer[@type='song']")
    return _make_instrument_track(
        container, name, instrument, mixer_channel, volume, panning
    )


SURGE_XT_LV2_URI = "https://surge-synthesizer.github.io/lv2/surge-xt"
_SURGE_XT_STATE_FILE = Path(__file__).with_name("carla_surge_state.b64")

_CARLA_ENGINE_SETTINGS = {
    "ForceStereo": "false",
    "PreferPluginBridges": "false",
    "PreferUiBridges": "true",
    "UIsAlwaysOnTop": "false",
    "MaxParameters": "200",
    "UIBridgesTimeout": "4000",
    "LADSPA_PATH": "/home/dolf/.ladspa:/usr/lib/ladspa:/usr/local/lib/ladspa",
    "DSSI_PATH": "/home/dolf/.dssi:/usr/lib/dssi:/usr/local/lib/dssi",
    "LV2_PATH": "/home/dolf/.lv2:/usr/lib/lv2:/usr/local/lib/lv2",
    "VST2_PATH": "/home/dolf/.vst:/usr/lib/vst:/usr/local/lib/vst/home/dolf/.lxvst:/usr/lib/lxvst:/usr/local/lib/lxvst:/home/dolf/.wine/drive_c/Program Files/VstPlugins:/home/dolf/.wine/drive_c/Program Files (x86)/VstPlugins",
    "VST3_PATH": "/home/dolf/.vst3:/usr/lib/vst3:/usr/local/lib/vst3:/home/dolf/.wine/drive_c/Program Files/Common Files/VST3:/home/dolf/.wine/drive_c/Program Files (x86)/Common Files/VST3",
    "SF2_PATH": "/home/dolf/.sounds/sf2:/home/dolf/.sounds/sf3:/usr/share/sounds/sf2:/usr/share/sounds/sf3:/usr/share/soundfonts",
    "SFZ_PATH": "/home/dolf/.sounds/sfz:/usr/share/sounds/sfz",
    "JSFX_PATH": "/home/dolf/.config/REAPER/Effects",
}


def _surge_xt_state_string() -> str:
    """Return the exact Surge XT state captured from the working project."""
    try:
        encoded = _SURGE_XT_STATE_FILE.read_text(encoding="ascii")
        return zlib.decompress(base64.b64decode(encoded)).decode("utf-8")
    except (OSError, ValueError, zlib.error) as exc:
        raise RuntimeError("Packaged Carla/Surge XT state is unavailable") from exc


def _carla_plugin_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".vst3":
        return "VST3"
    if suffix == ".clap":
        return "CLAP"
    if suffix == ".lv2":
        return "LV2"
    raise ValueError("Carla supports only VST3 bundles, CLAP files, and LV2 bundles")


def _carla_state(path: Path, plugin_id: str = "") -> ET.Element:
    """Build the Carla project document accepted by CarlaInstrument::loadSettings."""
    plugin_type = _carla_plugin_type(path)
    identifier = plugin_id.strip()
    if plugin_type == "LV2":
        if not identifier or not identifier.startswith(("http://", "https://")):
            raise ValueError("LV2 plugins require their URI as plugin_id")
    elif path.name.lower() in {"surge xt.vst3", "surge xt.clap"}:
        raise ValueError("Surge XT must use its installed LV2 build through Carla")
    elif not identifier:
        raise ValueError(
            f"{plugin_type} plugin identifier is required; Carla cannot reliably "
            "select a plugin from the path alone"
        )

    state = ET.Element("CARLA-PROJECT", {"VERSION": "2.5"})
    if plugin_type == "LV2" and identifier == SURGE_XT_LV2_URI:
        engine = ET.SubElement(state, "EngineSettings")
        for name, value in _CARLA_ENGINE_SETTINGS.items():
            ET.SubElement(engine, name).text = value
    plugin = ET.SubElement(state, "Plugin")
    info = ET.SubElement(plugin, "Info")
    ET.SubElement(info, "Type").text = plugin_type
    ET.SubElement(info, "Name").text = "Surge XT" if identifier == SURGE_XT_LV2_URI else path.stem
    if plugin_type == "LV2":
        ET.SubElement(info, "URI").text = identifier
    else:
        ET.SubElement(info, "Binary").text = str(path)
        ET.SubElement(info, "Label" if plugin_type == "VST3" else "Identifier").text = identifier
    data = ET.SubElement(plugin, "Data")
    ET.SubElement(data, "Active").text = "Yes"
    if plugin_type == "LV2" and identifier == SURGE_XT_LV2_URI:
        ET.SubElement(data, "ControlChannel").text = "1"
    ET.SubElement(data, "Options").text = "0x3f1" if (
        plugin_type == "LV2" and identifier == SURGE_XT_LV2_URI
    ) else "0x0"
    if plugin_type == "LV2" and identifier == SURGE_XT_LV2_URI:
        custom = ET.SubElement(data, "CustomData")
        ET.SubElement(custom, "Type").text = "http://lv2plug.in/ns/ext/atom#String"
        ET.SubElement(custom, "Key").text = f"{SURGE_XT_LV2_URI}:StateString"
        ET.SubElement(custom, "Value").text = _surge_xt_state_string()
    return state


def load_carla_plugin(
    root: ET.Element,
    track_index: int,
    plugin_path: str | Path,
    plugin_id: str = "",
) -> dict:
    """Replace a Carla instrument's opaque state with a validated Carla project."""
    path = Path(plugin_path)
    if not path.exists():
        raise ValueError(f"Plugin not found: {plugin_path}")
    track = find_track_element(root, track_index)
    instrument = track.find("instrumenttrack/instrument")
    if instrument is None or instrument.get("name") not in {"carlarack", "carlapatchbay"}:
        raise ValueError(f"Track {track_index} is not a Carla instrument track")
    state = _carla_state(path, plugin_id)
    wrapper = instrument.find(instrument.get("name"))
    if wrapper is None:
        wrapper = ET.SubElement(instrument, instrument.get("name"))
    for child in list(wrapper):
        wrapper.remove(child)
    wrapper.append(state)
    ET.SubElement(wrapper, "key")
    return {
        "plugin": str(path),
        "plugin_type": state.findtext("Plugin/Info/Type"),
        "plugin_id": plugin_id,
    }


def configure_reference_carla_track(track: ET.Element) -> None:
    """Apply the LMMS track structure used by the working Carla project."""
    track.set("mutedBeforeSolo", "229")
    inst_track = track.find("instrumenttrack")
    if inst_track is None:
        raise ValueError("Carla track has no instrumenttrack")
    inst_track.attrib.clear()
    inst_track.attrib.update({
        "range_import": "1", "pitchrange": "1", "pan": "0",
        "usemasterpitch": "1", "keymap": "0", "pitch": "0",
        "lastkey": "127", "scale": "0", "enablecc": "0", "vol": "100",
        "mixch": "0", "enabled": "0", "basenote": "69", "firstkey": "0",
    })
    controllers = inst_track.find("midicontrollers")
    if controllers is None:
        controllers = ET.Element("midicontrollers")
        inst_track.insert(0, controllers)
    controllers.attrib.clear()
    controllers.attrib.update({f"cc{number}": "0" for number in range(128)})

    instrument = inst_track.find("instrument")
    wrapper = instrument.find("carlarack") if instrument is not None else None
    if wrapper is None:
        raise ValueError("Carla track has no carlarack instrument")
    wrapper.attrib.clear()
    wrapper.attrib.update({f"PARAM_KNOB_{number}": "0" for number in range(3, 110)})

    eldata = inst_track.find("eldata")
    if eldata is not None:
        eldata.attrib.clear()
        eldata.attrib.update({"fcut": "14000", "fwet": "0", "ftype": "0", "fres": "0.5"})
        for child in list(eldata):
            eldata.remove(child)
        envelope = {
            "lshp": "0", "lspd_denominator": "4", "dec": "0.5",
            "hold": "0.5", "lamt": "0", "lspd_numerator": "4",
            "ctlenvamt": "0", "x100": "0", "lpdel": "0", "rel": "0.1",
            "lspd_syncmode": "0", "amt": "0", "pdel": "0", "userwavefile": "",
            "latt": "0", "att": "0", "lspd": "0.1", "sustain": "0.5",
        }
        for name in ("elvol", "elcut", "elres"):
            ET.SubElement(eldata, name, envelope)
    chord = inst_track.find("chordcreator")
    if chord is not None:
        chord.attrib.clear()
        chord.attrib.update({"chord-enabled": "0", "chord": "0", "chordrange": "1"})
    arp = inst_track.find("arpeggiator")
    if arp is not None:
        arp.attrib.clear()
        arp.attrib.update({
            "arpskip": "0", "arptime_denominator": "4", "arpmode": "0",
            "arp": "0", "arp-enabled": "0", "arprepeats": "1", "arpdir": "0",
            "arpmiss": "0", "arptime_numerator": "4", "arptime": "200",
            "arprange": "1", "arpgate": "100", "arpcycle": "0",
            "arptime_syncmode": "0",
        })
    midiport = inst_track.find("midiport")
    if midiport is not None:
        midiport.attrib.clear()
        midiport.attrib.update({
            "outputprogram": "1", "basevelocity": "63", "fixedoutputnote": "-1",
            "inputcontroller": "-1", "fixedoutputvelocity": "-1", "readable": "0",
            "outputchannel": "1", "outputcontroller": "-1", "writable": "0",
            "fixedinputvelocity": "-1", "inputchannel": "0",
        })


def add_sample_track(
    root: ET.Element,
    name: str,
    mixer_channel: int = 0,
    volume: int = 100,
    panning: int = 0,
) -> ET.Element:
    """Add a sample track to the project."""
    song = root.find("song")
    container = song.find("trackcontainer[@type='song']")

    track = ET.SubElement(container, "track", {
        "muted": "0",
        "type": "2",
        "name": name,
        "solo": "0",
    })

    sample_track = ET.SubElement(track, "sampletrack", {
        "pan": str(panning),
        "vol": str(volume),
    })
    ET.SubElement(sample_track, "fxchain", {
        "numofeffects": "0", "enabled": "0",
    })

    return track


def add_automation_track(root: ET.Element, name: str = "Automation track") -> ET.Element:
    """Add an automation track to the project."""
    song = root.find("song")
    container = song.find("trackcontainer[@type='song']")

    track = ET.SubElement(container, "track", {
        "muted": "0",
        "type": "5",
        "name": name,
        "solo": "0",
    })

    ET.SubElement(track, "automationtrack")

    return track


def add_pattern_track(root: ET.Element, name: str = "Pattern 0") -> ET.Element:
    """Add a beat/bassline pattern track to the project.

    Creates one default inner instrument track (kicker) so notes can be
    added right away via add_note_to_track.
    """
    song = root.find("song")
    container = song.find("trackcontainer[@type='song']")

    track = ET.SubElement(container, "track", {
        "muted": "0",
        "type": "1",
        "name": name,
        "solo": "0",
    })

    patterntrack = ET.SubElement(track, "patterntrack")
    bb_container = ET.SubElement(patterntrack, "trackcontainer", {
        "width": "736", "x": "610", "y": "5", "maximized": "0",
        "height": "400", "visible": "0", "type": "patternstore",
        "minimized": "0",
    })
    _make_instrument_track(bb_container, name, instrument="kicker")

    return track


def _select_pattern(track: ET.Element, pattern_index: int | None) -> ET.Element | None:
    """Pick an instrument clip from a track (by index, else the first)."""
    patterns = track.findall("pattern") + track.findall("midiclip")
    if pattern_index is not None:
        if not 0 <= pattern_index < len(patterns):
            raise IndexError(
                f"Pattern index {pattern_index} out of range "
                f"(track has {len(patterns)} patterns)"
            )
        return patterns[pattern_index]
    return patterns[0] if patterns else None


def add_note_to_track(
    root: ET.Element,
    track_index: int,
    key: int = 60,
    pos: int = 0,
    length: int = 48,
    volume: int = 100,
    panning: int = 0,
    pattern_name: str | None = None,
    pattern_index: int | None = None,
) -> dict:
    """Add a note to a pattern on an instrument or pattern (BB) track.

    Creates the pattern if it doesn't exist. Note `pos` is relative to
    the pattern start. If the note ends beyond the pattern clip's length,
    the clip is automatically extended - LMMS does NOT play notes outside
    the clip window, so without this the notes would be silent.

    Returns a dict with the note element and whether the pattern length
    was extended.
    """
    tracks = find_tracks(root)
    if track_index >= len(tracks):
        raise IndexError(f"Track index {track_index} out of range (have {len(tracks)} tracks)")

    track = tracks[track_index]
    track_type = get_track_type(track)

    if track_type == 0:
        pattern = _select_pattern(track, pattern_index)
        if pattern is None:
            pname = pattern_name or track.get("name", "Pattern")
            pattern = ET.SubElement(track, "midiclip", {
                "type": "1", "off": "0", "muted": "0", "name": "",
                "steps": "16", "pos": "0", "autoresize": "1", "len": "192",
            })
    elif track_type == 1:
        container = track.find("patterntrack/trackcontainer")
        if container is None:
            container = track.find("bbtrack/trackcontainer")
        if container is None:
            raise ValueError("Pattern track has no trackcontainer")
        inner_tracks = container.findall("track")
        if not inner_tracks:
            raise ValueError("Pattern track has no inner instruments")
        pattern = _select_pattern(inner_tracks[0], pattern_index)
        if pattern is None:
            pname = pattern_name or "Pattern"
            pattern = ET.SubElement(inner_tracks[0], "pattern", {
                "len": "192", "muted": "0", "name": pname,
                "steps": "16", "pos": "0", "type": "1",
            })
    else:
        raise ValueError(f"Cannot add notes to track type {track_type_name(track_type)}")

    note = ET.SubElement(pattern, "note", {
        "len": str(length),
        "key": str(key),
        "vol": str(volume),
        "pos": str(pos),
        "pan": str(panning),
    })

    # Extend the clip so the note actually falls inside its window.
    old_len = _to_int(pattern.get("len", "192"), 192)
    extended = False
    new_len = old_len
    note_end = pos + length
    if note_end > old_len:
        new_len = note_end
        pattern.set("len", str(new_len))
        extended = True

    return {
        "note": note,
        "pattern_name": pattern.get("name", ""),
        "extended_len": extended,
        "old_len": old_len,
        "new_len": new_len,
    }


def add_bb_clip(
    root: ET.Element,
    track_index: int,
    position: int = 0,
    length: int = 768,
) -> ET.Element:
    """Add a beat/bassline clip to a pattern track in the song editor."""
    tracks = find_tracks(root)
    if track_index >= len(tracks):
        raise IndexError(f"Track index {track_index} out of range")

    track = tracks[track_index]
    if get_track_type(track) != 1:
        raise ValueError("Can only add BB clips to pattern tracks (type=1)")

    clip = ET.SubElement(track, "bbtco", {
        "len": str(int(length)),
        "muted": "0",
        "name": "",
        "usestyle": "1",
        "pos": str(int(position)),
        "color": "4282417407",
    })

    return clip


def add_mixer_channel(
    root: ET.Element,
    name: str,
    volume: float = 1.0,
) -> ET.Element:
    """Add a new mixer channel to the project."""
    song = root.find("song")
    mixer = song.find("mixer")
    if mixer is None:
        raise ValueError("No mixer element found")

    existing = mixer.findall("mixerchannel")
    num = max(int(ch.get("num", "0")) for ch in existing) + 1

    channel = ET.SubElement(mixer, "mixerchannel", {
        "num": str(num),
        "muted": "0",
        "volume": str(volume),
        "name": name,
        "soloed": "0",
    })
    ET.SubElement(channel, "fxchain", {"numofeffects": "0", "enabled": "0"})
    # Route the channel to Master. LMMS DELETES the implicit send to
    # master when it allocates channels during load
    # (Mixer::allocateChannelsTo) and only re-creates it if a <send>
    # element exists - without this, every sub-channel renders silent.
    ET.SubElement(channel, "send", {"channel": "0", "amount": "1"})

    return channel


def set_head_attribute(root: ET.Element, attr: str, value: str) -> None:
    """Set an attribute on the <head> element."""
    head = root.find("song/head")
    if head is None:
        head = root.find("head")
    if head is not None:
        head.set(attr, value)


# ──────────────────────────────────────────────────────────────────
# ARRANGEMENT: SAMPLE CLIPS & PATTERN PLACEMENT
# ──────────────────────────────────────────────────────────────────

def add_sample_clip(
    root: ET.Element,
    track_index: int,
    src: str,
    position: int = 0,
    length: int = 192,
) -> ET.Element:
    """Add a sample clip (audio file placement) to a sample track.

    Args:
        root: Project root element.
        track_index: Index of the sample track.
        src: Audio file path (relative to LMMS samples dir or absolute).
        position: Start position in ticks.
        length: Clip length in ticks.
    """
    tracks = find_tracks(root)
    if not 0 <= track_index < len(tracks):
        raise IndexError(f"Track index {track_index} out of range")
    track = tracks[track_index]
    if get_track_type(track) != 2:
        raise ValueError(
            f"Track {track_index} is not a sample track "
            f"(type={get_track_type(track)}). Use add_sample_track first."
        )

    clip = ET.SubElement(track, "sampleclip", {
        "pos": str(int(position)),
        "len": str(int(length)),
        "src": src,
        "muted": "0",
        "off": "0",
    })
    return clip


def place_instrument_pattern(
    root: ET.Element,
    track_index: int,
    position: int,
    name: str | None = None,
    length: int = 192,
) -> ET.Element:
    """Place an empty pattern clip at a position on an instrument track.

    This is the song-editor arrangement: each <pattern> element's pos
    attribute determines when it plays.

    Args:
        root: Project root element.
        track_index: Index of the instrument track.
        position: Start position in ticks (192 = 1 bar).
        name: Optional pattern name.
        length: Pattern length in ticks (default 192 = 1 bar).
    """
    tracks = find_tracks(root)
    if not 0 <= track_index < len(tracks):
        raise IndexError(f"Track index {track_index} out of range")
    track = tracks[track_index]
    if get_track_type(track) != 0:
        raise ValueError(
            f"Track {track_index} is not an instrument track "
            f"(type={get_track_type(track)})."
        )

    pname = name or f"{track.get('name', 'Pattern')}"
    pattern = ET.SubElement(track, "pattern", {
        "pos": str(int(position)),
        "len": str(int(length)),
        "name": pname,
        "muted": "0",
        "steps": "16",
        "type": "1",
    })
    return pattern


def get_arrangement(root: ET.Element) -> list[dict]:
    """Get all clips in the song editor arranged by time.

    Returns a list of clip dicts sorted by position: instrument patterns,
    BB clips (bbtco), sample clips and automation patterns.
    """
    tracks = find_tracks(root)
    clips = []
    for idx, track in enumerate(tracks):
        ttype = get_track_type(track)
        tname = track.get("name", "?")
        if ttype == 0:
            for pat in [*track.findall("pattern"), *track.findall("midiclip")]:
                notes = pat.findall("note")
                clips.append({
                    "track_index": idx, "track": tname,
                    "kind": "midiclip" if pat.tag == "midiclip" else "pattern",
                    "name": pat.get("name", ""),
                    "pos": _to_int(pat.get("pos", "0")),
                    "len": _to_int(pat.get("len", "192")),
                    "notes": len(notes),
                    "muted": pat.get("muted", "0") == "1",
                })
        elif ttype == 1:
            for bbtco in track.findall("bbtco"):
                clips.append({
                    "track_index": idx, "track": tname, "kind": "bbtco",
                    "name": bbtco.get("name", ""),
                    "pos": _to_int(bbtco.get("pos", "0")),
                    "len": _to_int(bbtco.get("len", "192")),
                    "notes": None,
                    "muted": bbtco.get("muted", "0") == "1",
                })
        elif ttype == 2:
            for sc in track.findall("sampleclip"):
                clips.append({
                    "track_index": idx, "track": tname, "kind": "sampleclip",
                    "name": Path(sc.get("src", "")).stem,
                    "pos": _to_int(sc.get("pos", "0")),
                    "len": _to_int(sc.get("len", "192")),
                    "notes": None,
                    "muted": sc.get("muted", "0") == "1",
                    "src": sc.get("src", ""),
                })
        elif ttype in (5, 6):
            for ap in track.findall("automationpattern"):
                clips.append({
                    "track_index": idx, "track": tname, "kind": "automation",
                    "name": ap.get("name", ""),
                    "pos": _to_int(ap.get("pos", "0")),
                    "len": _to_int(ap.get("len", "192")),
                    "notes": None,
                    "points": len(ap.findall("time")),
                    "muted": ap.get("mute", "0") == "1",
                })

    clips.sort(key=lambda c: (c["pos"], c["track_index"]))
    return clips


def move_clip(
    root: ET.Element,
    track_index: int,
    old_position: int,
    new_position: int,
) -> dict:
    """Move a clip (pattern/bbtco/sampleclip) to a new position."""
    tracks = find_tracks(root)
    if not 0 <= track_index < len(tracks):
        raise IndexError(f"Track index {track_index} out of range")
    track = tracks[track_index]

    tags = ["pattern", "midiclip", "bbtco", "sampleclip"]
    old_position = _to_int(old_position, -1)
    new_position = _to_int(new_position, 0)
    for tag in tags:
        for clip in track.findall(tag):
            if _to_int(clip.get("pos", "-1"), -1) == old_position:
                clip.set("pos", str(new_position))
                return {
                    "track_index": track_index,
                    "clip": tag,
                    "old_pos": old_position,
                    "new_pos": new_position,
                    "message": f"Moved {tag} from {old_position} to "
                               f"{new_position} ticks",
                }
    raise ValueError(
        f"No clip found at position {old_position} on track {track_index}"
    )


def delete_clip(root: ET.Element, track_index: int, position: int) -> dict:
    """Delete a clip at a given position from a track."""
    tracks = find_tracks(root)
    if not 0 <= track_index < len(tracks):
        raise IndexError(f"Track index {track_index} out of range")
    track = tracks[track_index]

    position = _to_int(position, -1)
    for tag in ["pattern", "midiclip", "bbtco", "sampleclip"]:
        for clip in track.findall(tag):
            if _to_int(clip.get("pos", "-1"), -1) == position:
                track.remove(clip)
                return {
                    "removed": tag,
                    "position": position,
                    "message": f"Deleted {tag} at position {position}",
                }
    raise ValueError(
        f"No clip found at position {position} on track {track_index}"
    )


# ──────────────────────────────────────────────────────────────────
# AUTOMATION SUPPORT
# ──────────────────────────────────────────────────────────────────

# Global ID counter for automatable models - IDs just need to be unique
# integers within the project; LMMS re-maps them on load via changeID().
_model_id_counter = [10000]


def next_model_id() -> int:
    """Allocate a unique model ID for automation references."""
    _model_id_counter[0] += 1
    return _model_id_counter[0]


def automate_attribute(
    parent: ET.Element,
    attr_name: str,
    current_value: str,
) -> int:
    """Convert an attribute into an automatable model element.

    LMMS saves automated values as child elements with an id instead of
    plain attributes:  <vol id="123" value="100"/>  instead of vol="100".
    The same id must be referenced by an automation pattern's <object>.

    Args:
        parent: Element holding the attribute (e.g. instrumenttrack).
        attr_name: Attribute name (e.g. "vol", "pan", "bpm").
        current_value: Current value string.

    Returns:
        The allocated model id.
    """
    # Already automated?
    existing = parent.find(attr_name)
    if existing is not None and existing.get("id"):
        return int(existing.get("id"))

    model_id = next_model_id()
    # Remove the plain attribute if present
    if attr_name in parent.attrib:
        del parent.attrib[attr_name]
    ET.SubElement(parent, attr_name, {
        "id": str(model_id),
        "value": current_value,
        "scale_type": "linear",
    })
    return model_id


AUTOMATION_TARGETS = {
    # key -> (description, value_range_hint)
    "tempo": ("Song tempo (BPM)", "10-999"),
    "master_volume": ("Master volume", "0-200"),
    "master_pitch": ("Master pitch (semitones)", "-12 to +12"),
}


def add_automation_track(
    root: ET.Element,
    name: str,
    points: list[tuple[int, float]],
    target_id: int | None = None,
    progression: int = 0,
    tension: float = 1.0,
    position: int = 0,
) -> ET.Element:
    """Add an automation track with a pattern (curve).

    Args:
        root: Project root element.
        name: Automation track/pattern name (e.g. "Volume swell").
        points: List of (tick_position, value) tuples defining the curve.
        target_id: Model id to automate (from automate_attribute), or
            None for an unlinked curve.
        progression: 0=linear, 1=cubic hermite (smooth curves).
        tension: Curve tension for smooth interpolation (default 1.0).
        position: Pattern start offset in ticks.

    Returns:
        The created track element.
    """
    if not points:
        raise ValueError("At least one point is required")

    song = root.find("song")
    container = song.find("trackcontainer[@type='song']")

    track = ET.SubElement(container, "track", {
        "muted": "0",
        "type": "5",
        "name": name,
        "solo": "0",
    })
    ET.SubElement(track, "automationtrack")

    max_pos = max(p[0] for p in points)
    pattern = ET.SubElement(track, "automationpattern", {
        "pos": str(position),
        "len": str(max(192, max_pos + 1)),
        "name": name,
        "prog": str(progression),
        "tens": str(tension),
        "mute": "0",
        "off": "0",
        "autoresize": "1",
    })

    for pt_pos, value in sorted(points):
        ET.SubElement(pattern, "time", {
            "pos": str(int(pt_pos)),
            "value": str(value),
            "outValue": str(value),
            "inTan": "0",
            "outTan": "0",
            "lockedTan": "0",
        })

    if target_id is not None:
        ET.SubElement(pattern, "object", {"id": str(target_id)})

    return track


def resolve_automation_target(
    root: ET.Element,
    target_type: str,
    target_index: int,
    param: str,
) -> tuple[int, float]:
    """Resolve an automation target to (model_id, current_value).

    Args:
        root: Project root element.
        target_type: One of "track", "mixer", "song".
        target_index: Track/mixer channel index (ignored for "song").
        param: Parameter to automate:
            - track: "volume" or "panning"
            - mixer: "volume"
            - song: "tempo", "master_volume", "master_pitch"

    Returns:
        Tuple of (model_id, current_value).

    Raises:
        ValueError: If the target/param combination is invalid.
    """
    if target_type == "song":
        head = root.find("song/head")
        if head is None:
            head = root.find("head")
        attr_map = {
            "tempo": ("bpm", "140"),
            "master_volume": ("mastervol", "100"),
            "master_pitch": ("masterpitch", "0"),
        }
        if param not in attr_map:
            raise ValueError(
                f"Unknown song parameter '{param}'. Valid: "
                f"{sorted(attr_map.keys())}"
            )
        attr, default = attr_map[param]
        value = head.get(attr, default)
        model_id = automate_attribute(head, attr, value)
        return model_id, float(value)

    if target_type == "track":
        track = find_track_element(root, target_index)
        inst_track = track.find("instrumenttrack")
        if inst_track is None:
            st = track.find("sampletrack")
            if st is None:
                raise ValueError(
                    f"Track {target_index} has no automatable volume/panning"
                )
            parent, vol_attr, pan_attr = st, "vol", "pan"
        else:
            parent, vol_attr, pan_attr = inst_track, "vol", "pan"

        if param == "volume":
            value = parent.get(vol_attr, "100")
            return automate_attribute(parent, vol_attr, value), float(value)
        if param == "panning":
            value = parent.get(pan_attr, "0")
            return automate_attribute(parent, pan_attr, value), float(value)
        raise ValueError(
            f"Unknown track parameter '{param}'. Valid: volume, panning"
        )

    if target_type == "mixer":
        song = root.find("song")
        mixer = song.find("mixer")
        channels = mixer.findall("mixerchannel")
        if not 0 <= target_index < len(channels):
            raise ValueError(
                f"Mixer channel {target_index} out of range "
                f"(0-{len(channels) - 1})"
            )
        channel = channels[target_index]
        if param != "volume":
            raise ValueError(
                f"Unknown mixer parameter '{param}'. Valid: volume"
            )
        value = channel.get("volume", "1")
        return automate_attribute(channel, "volume", value), float(value)

    raise ValueError("target_type must be 'track', 'mixer' or 'song'")


# ──────────────────────────────────────────────────────────────────
# INSTRUMENT PARAMETER / PRESET SUPPORT
# ──────────────────────────────────────────────────────────────────

def find_track_element(root: ET.Element, index: int) -> ET.Element:
    """Find a track element by its position in the song trackcontainer."""
    song = root.find("song")
    container = song.find("trackcontainer[@type='song']")
    tracks = container.findall("track")
    if not 0 <= index < len(tracks):
        raise ValueError(
            f"Track index {index} out of range (0-{len(tracks) - 1})"
        )
    return tracks[index]


def set_instrument_params(
    root: ET.Element,
    track_index: int,
    params: dict,
) -> dict:
    """Set parameters on the <instrument> element of an instrument track.

    For zynaddsubfx these are: portamento, filterfreq, filterq, bandwidth,
    fmgain, rescenterfreq, resbandwidth, modifiedcontrollers.
    """
    track = find_track_element(root, track_index)
    inst_track = track.find("instrumenttrack")
    if inst_track is None:
        raise ValueError(f"Track {track_index} is not an instrument track")
    inst = inst_track.find("instrument")
    if inst is None:
        raise ValueError(f"Track {track_index} has no instrument element")

    applied = {}
    for key, value in params.items():
        inst.set(key, str(value))
        applied[key] = value

    return {
        "track_index": track_index,
        "instrument": inst.get("name"),
        "applied": applied,
    }


def embed_zyn_preset(
    root: ET.Element,
    track_index: int,
    preset_xml: str,
    preset_name: str = "",
) -> dict:
    """Embed ZynAddSubFX preset XML into a zynaddsubfx instrument track.

    Args:
        root: Project root element.
        track_index: Index of the target instrument track.
        preset_xml: Raw XML string from a .xiz preset file.
        preset_name: Optional display name for the result message.

    Returns:
        Dict with result info.

    Raises:
        ValueError: If track is not a zynaddsubfx instrument or the
            preset XML is invalid.
    """
    track = find_track_element(root, track_index)
    inst_track = track.find("instrumenttrack")
    if inst_track is None:
        raise ValueError(f"Track {track_index} is not an instrument track")
    inst = inst_track.find("instrument")
    if inst is None or inst.get("name") != "zynaddsubfx":
        actual = inst.get("name") if inst is not None else "none"
        raise ValueError(
            f"Track {track_index} uses '{actual}', not 'zynaddsubfx'. "
            f"Zyn presets can only be loaded on zynaddsubfx tracks."
        )

    # Validate and normalize the preset XML: strip leading whitespace and
    # the DOCTYPE (ElementTree cannot parse it and LMMS does not need it).
    # Many factory .xiz files start with a newline before <?xml ...?>,
    # which violates the XML spec and breaks strict parsers.
    import re as _re
    cleaned = _re.sub(r"<!DOCTYPE[^>]*>", "", preset_xml)
    cleaned = cleaned.lstrip()
    try:
        preset_root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid preset XML: {exc}") from exc

    if preset_root.tag != "ZynAddSubFX-data":
        raise ValueError(
            f"Preset root element is '{preset_root.tag}', expected "
            f"'ZynAddSubFX-data'. Is this really a .xiz file?"
        )

    # Remove any existing embedded data, then append the new one
    for old in inst.findall("ZynAddSubFX-data"):
        inst.remove(old)
    inst.append(preset_root)

    # Extract patch name from preset INFO if available
    patch_name = preset_name
    try:
        info = preset_root.find("INSTRUMENT/INFO")
        if info is not None:
            name_el = info.find("string[@name='name']")
            if name_el is not None and name_el.text:
                patch_name = name_el.text.strip()
    except (AttributeError, TypeError):
        pass

    return {
        "track_index": track_index,
        "preset": patch_name or preset_name or "unknown",
        "message": f"Loaded ZynAddSubFX preset '{patch_name}' into track "
                   f"{track_index}",
    }
