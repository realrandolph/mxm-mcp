# MXM MCP Server

An MCP (Model Context Protocol) server for [MXM](https://github.com/realrandolph/mxm) — the modern, cross-platform digital audio workstation. Enables AI agents to compose, edit, arrange, automate, sound-design, and inspect MXM music projects programmatically.

---

## Features

- **Project Management**: Create, load, inspect, and save compressed (`.mmpz`) and XML (`.mmp`) projects.
- **Track Types**: Instrument, Sample, Beat/Bassline Pattern, and Automation tracks.
- **Deterministic Musical Unit Composition**:
  - `add_phrase`: Motifs and repeating phrases with exact per-occurrence transforms (transpose, stretch, reverse, quantize, velocity).
  - `add_chord`: Pitch lists or deterministic chord symbols (e.g. `C4maj`, `A3min7/G3`, inversions, drop-2/drop-3 voicings).
  - `add_progression`: Sequential and absolute chord progressions across timeline bars/ticks.
  - `add_rhythm`: Pattern generation from step grids and hit lists on instrument or Beat/Bassline tracks.
  - `arrange_musical_units`: Batch arrangement of phrases, chords, progressions, and rhythms across song sections.
  - `edit_notes_batch`: Batch transformations with key/time filters, transpose, time-shift, stretch, reverse, and quantize.
  - `add_automation_batch` & `edit_automation`: Predictable parameter curve generation and modification with clamp/error policies.
- **Native VST3 & LV2 Hosting**: Direct native hosting of Surge XT, TalkingBass, and filesystem-discovered VST3/LV2 instruments without Carla.
- **Plugin & Preset Discovery**: Headless discovery for installed VST3 bundles and LV2 RDF metadata with persistent disk caching.
- **Effects Chain**: 18 built-in effects on tracks and mixer channels (delay, reverb, EQ, compressor, waveshaper...).
- **ZynAddSubFX Integration**: Browse ~950 factory presets (`.xiz`) and fine-tune synthesis parameters.
- **Mixer & Routing**: Create channels, route tracks (`mixch`), configure sends, and set volume/panning.
- **Headless Audio Rendering**: Export completed projects to WAV, FLAC, OGG, or MP3 via the MXM DAW engine.

---

## Installation

### Pre-Built Packages (Recommended)

Pre-built standalone installers are available from the repository [Releases](https://github.com/realrandolph/mxm-mcp/releases). They bundle the Python runtime and dependencies so no separate Python installation is required.

#### Linux (Linux Mint, Ubuntu, Debian)
1. Download `mxm-mcp-linux-x86_64.tar.gz` from the latest release.
2. Extract the archive:
   ```bash
   tar -xzf mxm-mcp-linux-x86_64.tar.gz
   cd mxm-mcp-linux-x86_64
   ```
3. Double-click `install.sh` in your file manager (such as Nemo or Nautilus), or run:
   ```bash
   ./install.sh
   ```
4. The installer provides a graphical Zenity wizard (with automatic terminal fallback) that:
   - Checks for MXM DAW and links to the releases page if missing.
   - Automatically detects installed harnesses (OpenCode, Codex, Claude).
   - Pre-selects all detected harnesses with an option to opt out.
   - Installs `mxm-mcp` to `~/.local/bin/mxm-mcp` and ensures it is in `PATH`.

#### Windows 10+
1. Download `mxm-mcp-setup-windows-x64.exe` from the latest release.
2. Run the installer executable.
3. The wizard will:
   - Check for MXM DAW.
   - Pre-select detected AI harnesses (OpenCode, Codex, Claude Desktop).
   - Install to `%LOCALAPPDATA%\Programs\mxm-mcp` (per-user install, no administrator privileges required).
   - Add the directory to the User `PATH` and register an uninstaller.

---

### MXM DAW Integration

MXM MCP can compose, edit, arrange, and save projects completely headlessly without the DAW application installed.

To render projects to audio files (WAV, FLAC, OGG, MP3), load factory ZynAddSubFX presets, or run live plugin availability checks, install the **MXM DAW** application:

👉 **[Download MXM DAW Releases](https://github.com/realrandolph/mxm/releases)**

---

### Install from Source

Requirements: Python 3.10+

```bash
git clone https://github.com/realrandolph/mxm-mcp.git
cd mxm-mcp
pip install -e .
```

To include development and test dependencies:
```bash
pip install -e ".[dev]"
```

---

## Harness Configuration

If you install via the pre-built installer, detected harnesses are configured automatically. For manual setup:

### OpenCode
Add to `~/.config/opencode/opencode.json` (Linux) or `%USERPROFILE%\.config\opencode\opencode.json` (Windows):
```json
{
  "mcp": {
    "mxm": {
      "type": "local",
      "command": ["mxm-mcp"]
    }
  }
}
```

### Codex
Add to `~/.codex/config.json` (Linux) or `%USERPROFILE%\.codex\config.json` (Windows):
```json
{
  "mcpServers": {
    "mxm": {
      "command": "mxm-mcp",
      "args": []
    }
  }
}
```

### Claude Desktop
Add to `~/.config/Claude/claude_desktop_config.json` (Linux) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):
```json
{
  "mcpServers": {
    "mxm": {
      "command": "mxm-mcp",
      "args": []
    }
  }
}
```

### Automated Harness Configuration Helper
You can also run the bundled configuration script directly at any time:
```bash
python3 installer/configure_harnesses.py detect
python3 installer/configure_harnesses.py configure --binary ~/.local/bin/mxm-mcp --all
python3 installer/configure_harnesses.py status
```

---

## Tool Reference

### Project Management

| Tool | Description |
|------|-------------|
| `create_project` | Create a new empty MXM project |
| `load_project` | Load an existing `.mmpz` or `.mmp` file |
| `save_project` | Save the current project |
| `get_project_info` | Get project overview (tempo, tracks, mixer channels) |
| `get_project_xml` | Get raw XML of the current project |

### Track Operations

| Tool | Description |
|------|-------------|
| `add_instrument_track` | Add a synthesizer or sampler track |
| `add_sample_track` | Add an audio sample track |
| `add_automation_track` | Add a parameter automation track |
| `add_pattern_track` | Add a Beat/Bassline pattern track |
| `remove_track` | Remove a track by index |
| `get_track` | Get detailed track information |
| `list_tracks` | List all tracks with summary |
| `set_track_volume` | Set track volume (0–200) |
| `set_track_panning` | Set track panning (-100 to +100) |
| `mute_track` | Mute or unmute a track |
| `solo_track` | Solo or unsolo a track |

### Notes & Patterns

| Tool | Description |
|------|-------------|
| `add_note` | Add a note by MIDI key number |
| `add_note_by_name` | Add a note by name (e.g. `C4`, `A#3`, `Bb2`) |
| `add_notes_batch` | Add multiple notes in a single call |

### Musical Unit Composition & Deterministic Editing

| Tool | Description |
|------|-------------|
| `add_phrase` | Place and repeat motifs/phrases with exact transforms (transpose, stretch, reverse, quantize, velocity) |
| `add_chord` | Place chords via explicit pitches or deterministic symbols (e.g. `C4maj`, `A3min7/G3`, inversions, drop2/drop3 voicings) |
| `add_progression` | Place sequential or absolute chord progressions across timeline bars/ticks |
| `add_rhythm` | Place rhythmic patterns from step grids or hit lists on instrument or Beat/Bassline tracks |
| `arrange_musical_units` | Batch arrange phrases, chords, progressions, and rhythms across song sections |
| `edit_notes_batch` | Batch transform existing notes with key/time-range filters, transpose, time-shift, stretch, reverse, quantize |
| `add_automation_batch` | Batch create parameter curves (tempo, volume, panning, pitch) with validation and clamp/error policies |
| `edit_automation` | Transform, scale, shift, clamp, reverse, or replace points on existing automation curves |

### Mixer & Routing

| Tool | Description |
|------|-------------|
| `add_mixer_channel` | Create a new mixer channel |
| `get_mixer_channels` | List all mixer channels and their settings |
| `set_mixer_channel_volume` | Set channel volume (0–200) |
| `set_mixer_channel_name` | Rename a mixer channel |
| `set_track_mixer_channel` | Route an instrument or sample track to an FX channel (`mixch`) |

### Song Settings

| Tool | Description |
|------|-------------|
| `set_tempo` | Set song BPM (10–999) |
| `set_time_signature` | Set time signature (e.g. 4/4, 3/4) |
| `set_master_volume` | Set master volume (0–200) |
| `set_master_pitch` | Set master pitch (-12 to +12 semitones) |

### Effects (FX Chain)

| Tool | Description |
|------|-------------|
| `add_effect` | Add a built-in effect to a track or mixer channel |
| `remove_effect` | Remove an effect by name or chain position |
| `toggle_effect` | Enable or bypass an effect |
| `get_effect_chain` | List all effects on a track or mixer channel |

Built-in effects: `amplifier`, `bassbooster`, `bitcrush`, `compressor`, `crossovereq`, `delay`, `dispersion`, `dualfilter`, `dynamicsprocessor`, `eq`, `flanger`, `frequencyshifter`, `multitapecho`, `reverbsc`, `slewdistortion`, `stereoenhancer`, `stereomatrix`, `waveshaper`.

### ZynAddSubFX Presets & Parameters

| Tool | Description |
|------|-------------|
| `list_zyn_presets` | Browse ~950 factory presets (`.xiz`) by category |
| `load_zyn_preset` | Load a factory preset into a ZynAddSubFX track |
| `set_zyn_params` | Tune portamento, filter, FM gain, resonance, and bandwidth |

### Arrangement (Song Editor Timeline)

| Tool | Description |
|------|-------------|
| `place_pattern` | Place an empty pattern clip on an instrument track |
| `place_bb_clip` | Trigger a Beat/Bassline pattern clip at a given time |
| `place_sample_clip` | Place an audio file clip on a sample track |
| `assign_sample_file` | Assign or replace the audio file on a sample clip |
| `move_clip` | Move a clip to a new position |
| `delete_clip` | Delete a clip at a position |
| `get_arrangement` | Full timeline overview of all clips sorted by position |

### Automation

| Tool | Description |
|------|-------------|
| `add_automation` | Create automation curves for tempo, master volume/pitch, track volume/panning, and mixer channel volume |

### App Integration

| Tool | Description |
|------|-------------|
| `get_mxm_info` | Detect installed MXM DAW version, executable path, and available plugins |
| `render_project` | Export current project to WAV, FLAC, OGG, or MP3 via headless MXM render |

### Native VST3, LV2, Plugins & Patch Design

| Tool | Description |
|------|-------------|
| `list_instruments` | List installed plugins (VST3, LV2) and built-in generators, excluding quarantined crashes |
| `list_presets` | List presets, vendor patch trees, and multi-voice banks (.syx#0..31, .fxb, .nksf, .vital...) |
| `plugin_params` | Introspect VST3 controller parameters with units, display text, and natural string ranges |
| `save_preset` | Save a sound as a reusable `.vstpreset` file with natural text or numeric parameter overrides |
| `patch_card` | Headlessly probe and measure a patch's acoustic profile (attack, tail, pitch drift, spectrum) |
| `list_available_plugins` | Dynamically list all plugins installed in your MXM build |
| `list_vst3_instruments` | Discover native VST3 plugins for MXM's built-in VST3 host |
| `add_vst3_instrument_track` | Add a track hosted by MXM's native VST3 host |
| `list_native_plugins` | Enumerate filesystem-discovered VST3 and LV2 plugins with pagination and compact records |
| `inspect_native_plugin` | Inspect a discovered VST3/LV2 plugin by name or identity |
| `add_lv2_instrument_track` | Add an installed instrument with MXM's native LV2 host |
| `list_plugin_presets` | Search installed VST3 and RDF-declared LV2 preset banks |
| `load_native_plugin_preset` | Embed supported VST3/LV2 preset state into a project |
| `refresh_native_plugin_discovery` | Clear cached indexes and rescan plugins/presets |
| `scan_vst_directory` | Find legacy VST2 `.dll` files in a folder |
| `add_vst_track` | Add a track hosting a legacy VST2 plugin (Vestige) |

### Musical Utilities

| Tool | Description |
|------|-------------|
| `note_name_to_key` | Convert note name (e.g. `C4`) to MIDI key number |
| `key_to_note_name` | Convert MIDI key number to note name |
| `bars_to_ticks_converter` | Convert musical bars to project ticks |
| `ticks_to_bars_converter` | Convert project ticks to musical bars |
| `generate_scale` | Generate note names in a musical scale |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MXM_PROJECTS_DIR` | Current working directory | Default directory for project saves with relative paths |
| `MXM_EXECUTABLE` | Auto-detected | Path to the MXM DAW binary (for rendering and plugin verification) |
| `MXM_PLUGIN_DIR` | Auto-detected | MXM plugins directory (for plugin library scans) |
| `MXM_PRESETS_DIR` | Auto-detected | Path to the ZynAddSubFX presets directory |
| `MXM_VST3_PATH_ONLY` | None | When set, restricts native VST3 discovery to `$VST3_PATH` |

---

## Development

```bash
# Clone the repository
git clone https://github.com/realrandolph/mxm-mcp.git
cd mxm-mcp

# Install with development dependencies
uv sync --extra dev

# Run unit and integration tests
uv run pytest -m "not integration"

# Build standalone Linux installer package
bash scripts/build_linux_package.sh

# Build standalone Windows installer package (on Windows)
python scripts/build_windows_package.py
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.
