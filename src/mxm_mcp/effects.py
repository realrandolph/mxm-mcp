"""Effect chain (fxchain) handling for LMMS projects.

Control blocks are serialized from observed LMMS 1.3 output and from the
matching plugin saveSettings()/constructor defaults. Enabled effects are
never written with empty or guessed control nodes.
"""

from __future__ import annotations

import base64
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


def _b64_floats(*values: float) -> str:
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def _linear_transfer(length: int = 200) -> str:
    return _b64_floats(*((i + 1) / length for i in range(length)))


def _const_floats(length: int, value: float) -> str:
    return _b64_floats(*((value,) * length))


DEFAULT_TRANSFER_SHAPE = _linear_transfer(200)
DEFAULT_MULTITAP_AMP = _const_floats(16, 0.0)
DEFAULT_MULTITAP_LP = _const_floats(16, 3.0)


@dataclass(frozen=True)
class ControlParam:
    name: str
    default: float | int | str
    minimum: float | None = None
    maximum: float | None = None
    kind: str = "float"
    tempo_sync: bool = False
    description: str = ""


@dataclass(frozen=True)
class EffectSpec:
    name: str
    description: str
    controls_tag: str
    params: tuple[ControlParam, ...]
    source: str


def _p(
    name: str,
    default: float | int | str,
    minimum: float | None = None,
    maximum: float | None = None,
    kind: str = "float",
    tempo_sync: bool = False,
    description: str = "",
) -> ControlParam:
    return ControlParam(name, default, minimum, maximum, kind, tempo_sync, description)


EFFECT_SPECS: dict[str, EffectSpec] = {
    "amplifier": EffectSpec(
        "amplifier", "Volume/gain control", "AmplifierControls",
        (
            _p("volume", 100, 0, 200, description="Output volume"),
            _p("pan", 0, -100, 100, description="Panning"),
            _p("left", 100, 0, 200, description="Left gain"),
            _p("right", 100, 0, 200, description="Right gain"),
        ),
        "lmms-source-defaults",
    ),
    "bassbooster": EffectSpec(
        "bassbooster", "Low frequency booster", "bassboostercontrols",
        (
            _p("freq", 100, 50, 200, description="Boost frequency"),
            _p("ratio", 2, 0.1, 10, description="Boost ratio"),
            _p("gain", 1, 0.1, 5, description="Boost gain"),
        ),
        "lmms-saved",
    ),
    "bitcrush": EffectSpec(
        "bitcrush", "Bit crusher for lo-fi sound", "bitcrushcontrols",
        (
            _p("ingain", 0, -20, 20, description="Input gain dB"),
            _p("innoise", 0, 0, 100, description="Input noise"),
            _p("outgain", 0, -20, 20, description="Output gain dB"),
            _p("outclip", 0, -20, 20, description="Output clip dB"),
            _p("rate", 44100, 20, 44100, description="Resample rate"),
            _p("stereodiff", 0, -50, 50, description="Stereo difference"),
            _p("levels", 256, 1, 256, description="Quantization levels"),
            _p("rateon", 1, 0, 1, kind="bool", description="Rate crush enable"),
            _p("depthon", 1, 0, 1, kind="bool", description="Depth crush enable"),
        ),
        "lmms-source-defaults",
    ),
    "compressor": EffectSpec(
        "compressor", "Dynamic range compressor", "CompressorControls",
        (
            _p("threshold", -8, -60, 0, description="Threshold dB"),
            _p("ratio", 1.8, 1, 20, description="Compression ratio"),
            _p("attack", 10, 0, 200, description="Attack ms"),
            _p("release", 100, 1, 500, description="Release ms"),
            _p("knee", 12, 0, 24, description="Knee dB"),
            _p("hold", 0, 0, 500, description="Hold ms"),
            _p("range", -240, -240, 0, description="Range dB"),
            _p("rms", 1, 0, 1, kind="bool", description="RMS detection"),
            _p("peakmode", 0, 0, 1, kind="bool", description="Peak mode"),
            _p("limiter", 0, 0, 1, kind="bool", description="Limiter"),
            _p("feedback", 0, 0, 1, kind="bool", description="Feedback"),
            _p("autoAttack", 0, 0, 1, kind="bool"),
            _p("autoRelease", 0, 0, 1, kind="bool"),
            _p("autoMakeup", 0, 0, 1, kind="bool"),
            _p("audition", 0, 0, 1, kind="bool"),
            _p("lookahead", 0, 0, 1, kind="bool"),
            _p("lookaheadLength", 0, 0, 20),
            _p("mix", 100, 0, 100, description="Wet mix percent"),
            _p("blend", 1, 0, 1),
            _p("tilt", 0, -6, 6),
            _p("tiltFreq", 150, 20, 20000),
            _p("stereoLink", 1, 0, 1, kind="bool"),
            _p("midside", 0, 0, 1, kind="bool"),
            _p("inGain", 0, -24, 24),
            _p("outGain", 0, -24, 24),
            _p("inBalance", 0, -1, 1),
            _p("outBalance", 0, -1, 1),
            _p("stereoBalance", 0, -1, 1),
        ),
        "lmms-saved",
    ),
    "crossovereq": EffectSpec(
        "crossovereq", "4-band crossover EQ", "crossoevereqcontrols",
        (
            _p("xover12", 125, 50, 10000, description="Band 1/2 crossover Hz"),
            _p("xover23", 1250, 50, 20000, description="Band 2/3 crossover Hz"),
            _p("xover34", 5000, 50, 20000, description="Band 3/4 crossover Hz"),
            _p("gain1", 0, -60, 30),
            _p("gain2", 0, -60, 30),
            _p("gain3", 0, -60, 30),
            _p("gain4", 0, -60, 30),
            _p("mute1", 1, 0, 1, kind="bool", description="Band 1 enable (misnamed mute in LMMS)"),
            _p("mute2", 1, 0, 1, kind="bool", description="Band 2 enable"),
            _p("mute3", 1, 0, 1, kind="bool", description="Band 3 enable"),
            _p("mute4", 1, 0, 1, kind="bool", description="Band 4 enable"),
        ),
        "lmms-source-defaults",
    ),
    "delay": EffectSpec(
        "delay", "Tempo-synced delay/echo", "Delay",
        (
            _p("DelayTimeSamples", 0.5, 0.01, 5, tempo_sync=True, description="Delay time"),
            _p("FeebackAmount", 0, 0, 1, description="Feedback (LMMS spelling)"),
            _p("LfoFrequency", 2, 0.01, 5, tempo_sync=True),
            _p("LfoAmount", 0, 0, 0.5, tempo_sync=True),
            _p("OutGain", 0, -60, 20),
        ),
        "lmms-saved",
    ),
    "dispersion": EffectSpec(
        "dispersion", "Allpass dispersion/phaser", "DispersionControls",
        (
            _p("amount", 0, 0, 32, kind="int", description="Filter count"),
            _p("freq", 200, 20, 20000),
            _p("reso", 0.707, 0.01, 8),
            _p("feedback", 0, -1, 1),
            _p("dc", 0, 0, 1, kind="bool", description="DC offset removal"),
        ),
        "lmms-source-defaults",
    ),
    "dualfilter": EffectSpec(
        "dualfilter", "Dual filter (parallel/serial)", "DualFilterControls",
        (
            _p("enabled1", 1, 0, 1, kind="bool"),
            _p("filter1", 0, 0, 21, kind="int", description="Filter 1 type"),
            _p("cut1", 7000, 1, 20000),
            _p("res1", 0.5, 0.01, 10),
            _p("gain1", 100, 0, 200),
            _p("mix", 0, -1, 1),
            _p("enabled2", 1, 0, 1, kind="bool"),
            _p("filter2", 0, 0, 21, kind="int"),
            _p("cut2", 7000, 1, 20000),
            _p("res2", 0.5, 0.01, 10),
            _p("gain2", 100, 0, 200),
        ),
        "lmms-saved",
    ),
    "dynamicsprocessor": EffectSpec(
        "dynamicsprocessor", "Dynamics processor (transfer curve)", "dynamicsprocessor_controls",
        (
            _p("inputGain", 1, 0, 5),
            _p("outputGain", 1, 0, 5),
            _p("attack", 10, 1, 500),
            _p("release", 100, 1, 500),
            _p("stereoMode", 0, 0, 2, kind="int"),
            _p("waveShape", DEFAULT_TRANSFER_SHAPE, kind="data", description="200-point transfer curve"),
        ),
        "lmms-source-defaults",
    ),
    "eq": EffectSpec(
        "eq", "Parametric equalizer", "Eq",
        (
            _p("Inputgain", 0, -60, 20),
            _p("Outputgain", 0, -60, 20),
            _p("HPactive", 0, 0, 1, kind="bool"),
            _p("HPfreq", 31, 20, 20000),
            _p("HPres", 0.70700002, 0.003, 10),
            _p("HP", 0, 0, 2, kind="int"),
            _p("HP12", 1, 0, 1, kind="bool"),
            _p("HP24", 0, 0, 1, kind="bool"),
            _p("HP48", 0, 0, 1, kind="bool"),
            _p("Lowshelfactive", 0, 0, 1, kind="bool"),
            _p("LowShelffreq", 80, 20, 20000),
            _p("Lowshelfgain", 0, -18, 18),
            _p("LowShelfres", 0.70700002, 0.55, 10),
            _p("Peak1active", 0, 0, 1, kind="bool"),
            _p("Peak1freq", 120, 20, 20000),
            _p("Peak1gain", 0, -18, 18),
            _p("Peak1bw", 0.30000001, 0.1, 4),
            _p("Peak2active", 0, 0, 1, kind="bool"),
            _p("Peak2freq", 250, 20, 20000),
            _p("Peak2gain", 0, -18, 18),
            _p("Peak2bw", 0.30000001, 0.1, 4),
            _p("Peak3active", 0, 0, 1, kind="bool"),
            _p("Peak3freq", 2000, 20, 20000),
            _p("Peak3gain", 0, -18, 18),
            _p("Peak3bw", 0.30000001, 0.1, 4),
            _p("Peak4active", 0, 0, 1, kind="bool"),
            _p("Peak4freq", 4000, 20, 20000),
            _p("Peak4gain", 0, -18, 18),
            _p("Peak4bw", 0.30000001, 0.1, 4),
            _p("Highshelfactive", 0, 0, 1, kind="bool"),
            _p("Highshelffreq", 12000, 20, 20000),
            _p("HighShelfgain", 0, -18, 18),
            _p("HighShelfres", 0.70700002, 0.55, 10),
            _p("LPactive", 0, 0, 1, kind="bool"),
            _p("LPfreq", 18000, 20, 20000),
            _p("LPres", 0.70700002, 0.003, 10),
            _p("LP", 0, 0, 2, kind="int"),
            _p("LP12", 1, 0, 1, kind="bool"),
            _p("LP24", 0, 0, 1, kind="bool"),
            _p("LP48", 0, 0, 1, kind="bool"),
            _p("AnalyseIn", 1, 0, 1, kind="bool"),
            _p("AnalyseOut", 1, 0, 1, kind="bool"),
        ),
        "lmms-saved",
    ),
    "flanger": EffectSpec(
        "flanger", "Flanger modulation effect", "Flanger",
        (
            _p("DelayTimeSamples", 0.001, 0.0001, 0.05),
            _p("LfoFrequency", 0.25, 0.01, 60, tempo_sync=True),
            _p("LfoAmount", 0, 0, 0.0025),
            _p("LfoPhase", 90, 0, 360),
            _p("Feedback", 0, -1, 1),
            _p("WhiteNoise", 0, 0, 0.05),
            _p("Invert", 0, 0, 1, kind="bool"),
        ),
        "lmms-saved",
    ),
    "frequencyshifter": EffectSpec(
        "frequencyshifter", "Frequency shifter", "FrequencyShifterControls",
        (
            _p("freqShift", 0),
            _p("lfoAmount", 0),
            _p("lfoRate", 0.2),
            _p("lfoStereoPhase", 0),
            _p("feedback", 0),
            _p("phase", 0),
            _p("mix", 1, 0, 1),
            _p("glide", 0.050000001),
            _p("tone", 22000),
            _p("harmonics", 0, 0, 1, kind="bool"),
            _p("spreadShift", 0),
            _p("antireflect", 0, 0, 1, kind="bool"),
            _p("ring", 0, 0, 1, kind="bool"),
            _p("routeMode", 0, kind="int"),
            _p("delayDamp", 22000),
            _p("delayGlide", 0.050000001),
            _p("delayLengthShort", 0),
            _p("m_delayLengthLong", 0),
        ),
        "lmms-saved",
    ),
    "multitapecho": EffectSpec(
        "multitapecho", "Multi-tap echo", "multitapechocontrols",
        (
            _p("steps", 16, 4, 32, kind="int"),
            _p("steplength", 100, 1, 500),
            _p("drygain", 0, -80, 20),
            _p("swapinputs", 0, 0, 1, kind="bool"),
            _p("stages", 1, 1, 4),
            _p("ampsteps", DEFAULT_MULTITAP_AMP, kind="data"),
            _p("lpsteps", DEFAULT_MULTITAP_LP, kind="data"),
        ),
        "lmms-source-defaults",
    ),
    "reverbsc": EffectSpec(
        "reverbsc", "Studio-quality reverb (Schroeder)", "ReverbSCControls",
        (
            _p("input_gain", 0, -60, 15),
            _p("size", 0.88999999, 0, 1),
            _p("color", 10000, 100, 15000),
            _p("output_gain", 0, -60, 15),
        ),
        "lmms-saved",
    ),
    "slewdistortion": EffectSpec(
        "slewdistortion", "Slew-rate distortion", "SlewDistortionControls",
        (
            _p("distType1", 0, kind="int"),
            _p("distType2", 0, kind="int"),
            _p("drive1", 0),
            _p("drive2", 0),
            _p("mix1", 1, 0, 1),
            _p("mix2", 1, 0, 1),
            _p("outVol1", 0),
            _p("outVol2", 0),
            _p("bias1", 0),
            _p("bias2", 0),
            _p("warp1", 0),
            _p("warp2", 0),
            _p("crush1", 0),
            _p("crush2", 0),
            _p("slewUp1", 6),
            _p("slewDown1", 6),
            _p("slewUp2", 6),
            _p("slewDown2", 6),
            _p("slewLink1", 1, 0, 1, kind="bool"),
            _p("slewLink2", 1, 0, 1, kind="bool"),
            _p("attack1", 2),
            _p("release1", 20),
            _p("attack2", 2),
            _p("release2", 20),
            _p("dynamics1", 0),
            _p("dynamics2", 0),
            _p("dynamicSlew1", 0),
            _p("dynamicSlew2", 0),
            _p("split", 200),
            _p("multiband", 0, 0, 1, kind="bool"),
            _p("dcRemove", 1, 0, 1, kind="bool"),
            _p("oversampling", 0, kind="int"),
        ),
        "lmms-saved",
    ),
    "stereoenhancer": EffectSpec(
        "stereoenhancer", "Stereo width enhancer", "stereoenhancercontrols",
        (
            _p("width", 0, 0, 180, description="Stereo width degrees"),
        ),
        "lmms-saved",
    ),
    "stereomatrix": EffectSpec(
        "stereomatrix", "Stereo channel matrix", "stereomatrixcontrols",
        (
            _p("l-l", 1, -1, 1, description="Left to left"),
            _p("l-r", 0, -1, 1, description="Left to right"),
            _p("r-l", 0, -1, 1, description="Right to left"),
            _p("r-r", 1, -1, 1, description="Right to right"),
        ),
        "lmms-source-defaults",
    ),
    "waveshaper": EffectSpec(
        "waveshaper", "Waveshaping distortion", "waveshapercontrols",
        (
            _p("inputGain", 1, 0, 5),
            _p("outputGain", 1, 0, 5),
            _p("clipInput", 0, 0, 1, kind="bool"),
            _p("waveShape", DEFAULT_TRANSFER_SHAPE, kind="data", description="200-point waveshape"),
        ),
        "lmms-source-defaults",
    ),
}

KNOWN_EFFECTS = {
    name: (spec.description, spec.controls_tag) for name, spec in EFFECT_SPECS.items()
}

UNSUPPORTED_EFFECTS: dict[str, str] = {}

EXTERNAL_EFFECTS = {
    "ladspaeffect": "LADSPA plugin host (depends on installed LADSPA libs)",
    "lv2effect": "LV2 plugin host (depends on installed LV2 plugins)",
    "vsteffect": "VST plugin host (requires a compatible VST)",
}

EFFECT_RECOMMENDATIONS = {
    "vocals": ["eq", "compressor", "reverbsc", "delay"],
    "drums": ["compressor", "eq", "waveshaper"],
    "bass": ["compressor", "eq", "bassbooster"],
    "synth": ["delay", "reverbsc", "flanger", "bitcrush"],
    "master": ["eq", "compressor", "stereoenhancer", "amplifier"],
}


def _format_value(param: ControlParam, value) -> str:
    if param.kind == "data":
        text = str(value)
        if not text:
            raise ValueError(f"Effect control '{param.name}' requires a non-empty data value")
        return text
    if param.kind == "bool":
        if isinstance(value, str):
            return "1" if value.strip() not in {"0", "false", "False"} else "0"
        return "1" if bool(value) else "0"
    if param.kind == "int":
        number = int(float(value))
        if param.minimum is not None and number < param.minimum:
            raise ValueError(f"{param.name}={number} is below minimum {param.minimum}")
        if param.maximum is not None and number > param.maximum:
            raise ValueError(f"{param.name}={number} is above maximum {param.maximum}")
        return str(number)
    number = float(value)
    if param.minimum is not None and number < param.minimum:
        raise ValueError(f"{param.name}={number} is below minimum {param.minimum}")
    if param.maximum is not None and number > param.maximum:
        raise ValueError(f"{param.name}={number} is above maximum {param.maximum}")
    if number.is_integer() and abs(number) < 1e12:
        return str(int(number))
    text = f"{number:.8g}"
    return text


def _controls_attributes(spec: EffectSpec, params: dict | None) -> dict[str, str]:
    overrides = {str(key): value for key, value in (params or {}).items()}
    unknown = set(overrides) - {param.name for param in spec.params}
    if unknown:
        valid = ", ".join(param.name for param in spec.params)
        raise ValueError(
            f"Unknown {spec.name} parameter(s): {sorted(unknown)}. Valid: {valid}"
        )
    attrs: dict[str, str] = {}
    for param in spec.params:
        value = overrides[param.name] if param.name in overrides else param.default
        attrs[param.name] = _format_value(param, value)
        if param.tempo_sync:
            attrs.setdefault(f"{param.name}_syncmode", "0")
            attrs.setdefault(f"{param.name}_numerator", "4")
            attrs.setdefault(f"{param.name}_denominator", "4")
    if not attrs:
        raise ValueError(f"Refusing to serialize {spec.name} without control state")
    return attrs


_EFFECT_MODEL_TAGS = {"on", "wet"}
_INSTRUMENT_LV2_URIS = {
    "https://opencode.local/lv2/talkingbass",
    "https://opencode.local/lv2/talkingbass#growl",
    "https://opencode.local/lv2/talkingbass#whisper",
}


def _set_effect_model(effect: ET.Element, name: str, value: str) -> None:
    from .xml_parser import next_model_id
    child = effect.find(name)
    if child is None:
        ET.SubElement(effect, name, {"id": str(next_model_id()), "value": value})
        return
    child.set("value", value)
    if not child.get("id") or child.get("id") == "0":
        child.set("id", str(next_model_id()))


def _get_effect_model(effect: ET.Element, name: str, default: str = "1") -> str:
    child = effect.find(name)
    if child is not None and child.get("value") is not None:
        return child.get("value")
    return effect.get(name, default)


def build_effect_element(
    effect_name: str,
    wet: float = 1.0,
    enabled: bool = True,
    params: dict | None = None,
) -> ET.Element:
    spec = get_effect_spec(effect_name)
    if not 0.0 <= float(wet) <= 1.0:
        raise ValueError("wet must be between 0.0 and 1.0")
    effect = ET.Element("effect", {
        "autoquit": "1",
        "autoquit_denominator": "4",
        "autoquit_numerator": "4",
        "autoquit_syncmode": "0",
        "name": spec.name,
    })
    _set_effect_model(effect, "on", "1" if enabled else "0")
    _set_effect_model(effect, "wet", _format_value(_p("wet", 1, 0, 1), wet))
    ET.SubElement(effect, spec.controls_tag, _controls_attributes(spec, params))
    ET.SubElement(effect, "key")
    return effect


def get_effect_spec(effect_name: str) -> EffectSpec:
    normalized = effect_name.strip().lower()
    if normalized in UNSUPPORTED_EFFECTS:
        raise ValueError(
            f"Effect '{normalized}' is not safely serializable: "
            f"{UNSUPPORTED_EFFECTS[normalized]}"
        )
    spec = EFFECT_SPECS.get(normalized)
    if spec is None:
        if normalized in EXTERNAL_EFFECTS:
            raise ValueError(
                f"Effect '{effect_name}' requires external plugins "
                f"({EXTERNAL_EFFECTS[normalized]}). Use a built-in effect instead."
            )
        valid = ", ".join(sorted(EFFECT_SPECS))
        raise ValueError(f"Unknown effect '{effect_name}'. Valid effects: {valid}")
    return spec


def describe_effects(effect_name: str | None = None) -> dict:
    if effect_name:
        spec = get_effect_spec(effect_name)
        return {
            "supported": True,
            "effect": _spec_to_dict(spec),
        }
    return {
        "supported": [_spec_to_dict(spec) for spec in EFFECT_SPECS.values()],
        "unsupported": [
            {"name": name, "reason": reason}
            for name, reason in sorted(UNSUPPORTED_EFFECTS.items())
        ],
        "external": EXTERNAL_EFFECTS,
        "recommendations_by_use_case": EFFECT_RECOMMENDATIONS,
    }


def _spec_to_dict(spec: EffectSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "controls_node": spec.controls_tag,
        "source": spec.source,
        "parameters": [
            {
                "name": param.name,
                "default": param.default if param.kind != "data" else "<binary>",
                "min": param.minimum,
                "max": param.maximum,
                "kind": param.kind,
                "tempo_sync": param.tempo_sync,
                "description": param.description,
            }
            for param in spec.params
        ],
    }


def get_fxchain(parent: ET.Element, create: bool = True) -> ET.Element:
    """Get or create the fxchain element under a track or mixer channel."""
    fxchain = parent.find("fxchain")
    if fxchain is None and create:
        fxchain = ET.SubElement(parent, "fxchain", {
            "numofeffects": "0", "enabled": "0",
        })
    return fxchain


def add_effect(
    parent: ET.Element,
    effect_name: str,
    wet: float = 1.0,
    enabled: bool = True,
    position: int | None = None,
    plugin_path: str | None = None,
    plugin_id: str | None = None,
    params: dict | None = None,
) -> dict:
    """Add an effect with a complete known-valid control block."""
    normalized = effect_name.strip().lower()
    if normalized in {"ladspaeffect", "lv2effect"}:
        if not plugin_path or not plugin_id:
            raise ValueError(f"{normalized} requires plugin_path and plugin_id")
        key_attrs = ({"file": Path(plugin_path).stem, "plugin": plugin_id}
                     if normalized == "ladspaeffect" else {"uri": plugin_id})
        if normalized == "lv2effect" and not plugin_id.startswith(("http://", "https://")):
            raise ValueError("lv2effect plugin_id must be an LV2 URI")
        return add_external_effect(parent, normalized, key_attrs, wet, enabled, position)

    spec = get_effect_spec(normalized)
    fxchain = get_fxchain(parent)
    existing = [e.get("name") for e in fxchain.findall("effect")]
    if spec.name in existing:
        raise ValueError(
            f"Effect '{spec.name}' already exists on this chain "
            f"(position {existing.index(spec.name)})."
        )

    effect = build_effect_element(spec.name, wet=wet, enabled=enabled, params=params)
    controls = effect.find(spec.controls_tag)
    if controls is None or not controls.attrib:
        raise ValueError(f"Refusing to add '{spec.name}' with an empty control block")

    if position is None or position >= len(fxchain):
        fxchain.append(effect)
        final_pos = len(fxchain) - 1
    else:
        fxchain.insert(position, effect)
        final_pos = max(0, position)

    num = len(fxchain.findall("effect"))
    fxchain.set("numofeffects", str(num))
    fxchain.set("enabled", "1")
    return {
        "effect": spec.name,
        "position": final_pos,
        "wet": wet,
        "enabled": enabled,
        "controls": dict(controls.attrib),
        "chain_size": num,
        "message": f"Added {spec.name} at position {final_pos} "
                   f"(chain now has {num} effects)",
    }


def add_external_effect(
    parent: ET.Element,
    effect_name: str,
    key_attrs: dict[str, str],
    wet: float = 1.0,
    enabled: bool = True,
    position: int | None = None,
) -> dict:
    """Add a LADSPA/LV2 host effect using LMMS's serialized sub-plugin key."""
    normalized = effect_name.strip().lower()
    if normalized not in {"ladspaeffect", "lv2effect"}:
        raise ValueError("Only ladspaeffect and lv2effect are external effects")
    uri = key_attrs.get("uri", "")
    if normalized == "lv2effect" and uri in _INSTRUMENT_LV2_URIS:
        raise ValueError(
            f"{uri} is an LV2 instrument; host it with lv2instrument, not lv2effect"
        )
    fxchain = get_fxchain(parent)
    effect = ET.Element("effect", {
        "name": normalized,
        "autoquit": "1",
        "autoquit_denominator": "4",
        "autoquit_numerator": "4",
        "autoquit_syncmode": "0",
    })
    _set_effect_model(effect, "on", "1" if enabled else "0")
    _set_effect_model(effect, "wet", str(wet))
    key = ET.SubElement(effect, "key")
    for name, value in key_attrs.items():
        ET.SubElement(key, "attribute", {"name": name, "value": str(value)})
    if position is None or position >= len(fxchain.findall("effect")):
        fxchain.append(effect)
        final_pos = len(fxchain.findall("effect")) - 1
    else:
        fxchain.insert(position, effect)
        final_pos = max(0, position)
    fxchain.set("numofeffects", str(len(fxchain.findall("effect"))))
    fxchain.set("enabled", "1")
    return {"effect": normalized, "position": final_pos, "chain_size": len(fxchain.findall("effect")), "plugin": key_attrs}


def remove_effect(parent: ET.Element, identifier: str | int) -> dict:
    """Remove an effect from a fxchain by name or position."""
    fxchain = parent.find("fxchain")
    if fxchain is None:
        raise ValueError("This target has no effect chain.")

    effects = fxchain.findall("effect")
    target = None
    if isinstance(identifier, int):
        if 0 <= identifier < len(effects):
            target = effects[identifier]
    else:
        for eff in effects:
            if eff.get("name", "").lower() == str(identifier).lower():
                target = eff
                break

    if target is None:
        names = [e.get("name", "?") for e in effects]
        raise ValueError(
            f"Effect '{identifier}' not found. Existing effects: {names}"
        )

    removed_name = target.get("name")
    fxchain.remove(target)
    remaining = fxchain.findall("effect")
    fxchain.set("numofeffects", str(len(remaining)))
    if len(remaining) == 0:
        fxchain.set("enabled", "0")

    return {
        "removed": removed_name,
        "chain_size": len(remaining),
        "message": f"Removed {removed_name} ({len(remaining)} effects remain)",
    }


def list_effects(parent: ET.Element) -> list[dict]:
    """List all effects in a fxchain element."""
    fxchain = parent.find("fxchain")
    if fxchain is None:
        return []
    result = []
    for pos, eff in enumerate(fxchain.findall("effect")):
        controls = next(
            (child for child in list(eff)
             if child.tag not in {"key", *_EFFECT_MODEL_TAGS}),
            None,
        )
        result.append({
            "position": pos,
            "name": eff.get("name", "unknown"),
            "enabled": _get_effect_model(eff, "on", "1") == "1",
            "wet": float(_get_effect_model(eff, "wet", "1")),
            "controls_node": None if controls is None else controls.tag,
            "controls": {} if controls is None else dict(controls.attrib),
        })
    return result


def set_effect_enabled(parent: ET.Element, identifier: str | int, enabled: bool) -> dict:
    """Enable/disable an effect in a fxchain by name or position."""
    fxchain = parent.find("fxchain")
    if fxchain is None:
        raise ValueError("This target has no effect chain.")
    effects = fxchain.findall("effect")
    target = None
    if isinstance(identifier, int):
        if 0 <= identifier < len(effects):
            target = effects[identifier]
    else:
        for eff in effects:
            if eff.get("name", "").lower() == str(identifier).lower():
                target = eff
                break
    if target is None:
        raise ValueError(f"Effect '{identifier}' not found.")
    _set_effect_model(target, "on", "1" if enabled else "0")
    if "on" in target.attrib:
        del target.attrib["on"]
    return {
        "effect": target.get("name"),
        "enabled": enabled,
        "message": f"{target.get('name')} {'enabled' if enabled else 'disabled'}",
    }
