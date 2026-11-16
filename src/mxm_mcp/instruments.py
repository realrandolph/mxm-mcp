"""Registry and canonicalization helpers for MXM built-in instruments."""

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
    """Return the canonical built-in ID for an ID or list-instruments name."""
    candidate = str(plugin).strip()
    if candidate.casefold().startswith("builtin:"):
        candidate = candidate.split(":", 1)[1].strip()
    canonical = {instrument.casefold(): instrument for instrument in BUILTIN_INSTRUMENTS}
    resolved = canonical.get(candidate.casefold())
    if resolved is None:
        # Friendly display names accepted by the instrument tool/listing.
        display_names = {
            "triple oscillator": "tripleoscillator",
            "three-oscillator subtractive synth": "tripleoscillator",
            "kick drum synthesizer": "kicker",
            "audio file player/sampler": "audiofileprocessor",
            "additive organ synthesizer": "organic",
            "physical modeling mallets": "malletsstk",
            "game boy sound chip emulator": "freeboy",
            "roland tb-303 style acid bass synth": "lb302",
            "powerful 3-oscillator polyphonic synth": "monstro",
            "nes 8-bit sound chip emulator": "nes",
            "opl3 fm synthesizer": "opulenz",
            "gus patch sampler": "patman",
            "soundfont (.sf2) sample player": "sf2player",
            "retro sound effect generator": "sfxr",
            "commodore 64 sid chip emulator": "sid",
            "beat slicer": "slicert",
            "vibrating string physical model": "vibedstrings",
            "4-oscillator wavetable-style synth": "watsyn",
            "expressive mono lead synth": "xpressive",
            "zynaddsubfx powerful feature-rich synth": "zynaddsubfx",
            "gig sample library player": "gigplayer",
            "bit-crushed wavetable synth": "bitinvader",
            "vst plugin host": "vestige",
        }
        resolved = display_names.get(candidate.casefold())
    if resolved is None:
        valid = ", ".join(sorted(BUILTIN_INSTRUMENTS))
        raise ValueError(f"unknown built-in instrument {plugin!r}; valid IDs: {valid}")
    return resolved


def builtin_instrument_display_name(plugin: str) -> str:
    """Return the built-in name format exposed by ``list_instruments``."""
    return f"builtin:{resolve_builtin_instrument(plugin)}"
