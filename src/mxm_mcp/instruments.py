"""Registry and canonicalization helpers for MXM built-in instruments."""

import re

BUILTIN_INSTRUMENTS = {
    "tripleoscillator": "Three-oscillator subtractive synth (default, always works)",
    "kicker": "Kick drum synthesizer",
    "audiofileprocessor": "Audio file player/sampler (WAV, OGG, etc.)",
    "organic": "Additive organ synthesizer",
    "malletsstk": "Physical modeling mallets (STK)",
    "freeboy": "Game Boy sound chip emulator (chiptune)",
    "lb302": "Roland TB-303 style acid bass synth",
    "monstro": "Powerful 3-oscillator polyphonic synth",
    "nes": "NES 8-bit sound chip emulator (chiptune)",
    "opulenz": "OPL3 FM synthesizer (Yamaha DX100 style)",
    "patman": "GUS patch sampler",
    "sf2player": "SoundFont (.sf2) sample player",
    "sfxr": "Retro sound effect generator (chiptune)",
    "sid": "Commodore 64 SID chip emulator (chiptune)",
    "slicert": "Beat slicer for chopping audio loops",
    "vibedstrings": "Vibrating string physical model",
    "watsyn": "4-oscillator wavetable-style synth",
    "xpressive": "Expressive mono lead synth",
    "zynaddsubfx": "ZynAddSubFX powerful feature-rich synth",
    "gigplayer": "GIG sample library player",
    "bitinvader": "Bit-crushed wavetable synth",
    "vestige": "VST plugin host (Windows only)",
}


def resolve_builtin_instrument(plugin: str) -> str:
    """Resolve a built-in ID or its registry description to its canonical ID."""
    candidate = str(plugin).strip()
    if candidate.casefold().startswith("builtin:"):
        candidate = candidate.split(":", 1)[1].strip()
    normalized = re.sub(r"[^a-z0-9]", "", candidate.casefold())
    resolved = next((instrument for instrument in BUILTIN_INSTRUMENTS
                     if re.sub(r"[^a-z0-9]", "", instrument.casefold()) == normalized), None)
    if resolved is None:
        # Descriptions are the registry's display labels; match them without
        # punctuation so case and formatting differences do not need aliases.
        resolved = next((instrument for instrument, description in BUILTIN_INSTRUMENTS.items()
                         if re.sub(r"[^a-z0-9]", "", description.casefold()) == normalized), None)
    if resolved is None:
        valid = ", ".join(sorted(BUILTIN_INSTRUMENTS))
        raise ValueError(f"unknown built-in instrument {plugin!r}; valid IDs: {valid}")
    return resolved


def builtin_instrument_display_name(plugin: str) -> str:
    """Return the built-in name format exposed by ``list_instruments``."""
    return f"builtin:{resolve_builtin_instrument(plugin)}"
