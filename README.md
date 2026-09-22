# Strawberry

A local desktop AI mascot: a cel-shaded crab that lives on the desktop and reacts to what happens on the machine.

- **`WIRING.md`** — the harness spec: message contract, `strawberryd`, doorways, build order, widget shell. Start here.
- **`PACKAGING.md`** — the plan for shipping her as `uv tool install strawberry` plus a standalone widget binary.
- **`strawberryd/`** — Python daemon (HTTP intake + websocket to the widget). `uv sync && uv run pytest`.
- **`widget/`** — Godot 4.7 desktop widget: transparent, always-on-top, click-through. Run with `bin/strawberry`.
- **Voice** — Piper TTS, on when `[speech] enabled = true` in `~/.config/strawberry/config.toml`. `bin/strawberry voices` lists or downloads voices, `bin/strawberry audition` compares them, `bin/strawberry say "…"` makes her talk.
- **The gate** — every sentence you say is sorted locally by `embeddinggemma` into chat, question or request (with a topic and a confidence) before anyone answers; `bin/strawberry route "skip this song"` shows the reading, `scripts/gate_check.py` runs the phrase set. A clear music command ("skip this song", "pause", "louder", "what song is this") she does herself through the Spotify MCP server in about a second, then tells you the fact plus a quip. `bin/strawberry tools` lists what she can reach, `[tools.servers.*]` adds servers. Requests with something to fill in ("play some Nina Simone", "queue up the live version") go to the thinker: Qwen with the same Spotify tools, while she says "On it." and thinks; she then reports the fact in one sentence. A general question ("who was the president in 1960") goes to Qwen too, answered from memory, no internet. When she is only fairly sure you meant a request she asks "Want me to do that?", and a yes within ten seconds does it. She remembers the last few minutes of conversation and nothing more (WIRING §8b).
- **Names she can hear** — the recogniser is told the artists and playlists in your Spotify library, refreshed every ten minutes, and any names you list under `[voice] vocabulary`.
- **Type to her** — right-click → *Chat with Strawberry…* (or press T) opens a text field under her; Enter sends the line through the daemon exactly like a spoken sentence (gate, reflexes, Qwen) and she answers on the desktop. `bin/strawberry talk` does the same from a terminal, with the routing shown underneath.
- **Talk to her** — `bin/strawberry hotkey` binds Super+Shift+Space: press, speak, she listens (faster-whisper on the CPU) and answers out loud.
- **Daily use** — `bin/strawberry install` makes her start on login (systemd user units + autostart). Right-click her for mute, quiet hour, volume, skin, always-on-top, the settings file, and quit. Her pupils follow the mouse, and she dances to the actual beat of whatever is playing (techno gets a rave, metal a headbang, hip hop a groove).
- **`v2/`** — the current 3D asset (`strawberry_v2.glb`), its Blender build, and the cel preview project. See `v2/README.md`.
- Below: the original v1 asset notes, kept for the build/validation history.

Phase 1 evidence: `scripts/check_phase1.sh` → `widget/widget_checks.json`, `widget/capture_phase1.png`.

---

# Strawberry v1

Main deliverable: `strawberry_v1.glb` (about 431 KiB). Editable source: `strawberry_v1.blend`.
Built and round-trip tested in Blender 5.2.2 LTS. No external assets, textures, plugins, or Python dependencies are required by the build scripts. Per-face planar UVs support Godot's default tangent generation; materials remain untextured.

## Contract

- 16 mesh objects, one armature `arm_strawberry`, six bones: `root`, `body`, `eyestalk_L`, `eyestalk_R`, `claw_arm_L`, `claw_arm_R`.
- Every vertex has exactly one bone influence at weight 1. Object transforms are applied before binding and checked again at export.
- Six flat-colour materials with the specified names and sRGB colours. No textures, camera, or lights in the GLB.
- `mesh_eye_L` and `mesh_eye_R` each contain both the eye and pupil, with `blink` and optional `squint` morphs.
- `mesh_claw_lower_L` has `claw_open_L`; `mesh_claw_lower_R` has `claw_open_R`. Maximum opening is 48 degrees, represented as vertex morph deltas.
- `mesh_shell` and `mesh_spots` each have `squash`. Set both together for a runtime squash. The breathing clip already animates both.
- Animation names: `idle_loop`, `listen_loop`, `think_loop`, `talk_base`, `alert_snap`, `notify_perk`.
- 30 fps; frame ranges 1–90, 1–60, 1–60, 1–40, 1–20, and 1–20 respectively. Loop endpoints match exactly. Duration is (last frame − first frame) / 30.
- No claw morph animation is baked into any clip, including `talk_base`. Blink and squint are also available for runtime control.
- Front in Blender is +Y; export converts to standard glTF +Y-up. Final character bounds are approximately 1.06 × 0.42 × 0.68 metres.

## Godot 4 hookup

Copy the GLB into your Godot project. In its Import dock, disable **Nodes → Use Name Suffixes** (`nodes/use_name_suffixes=false`) and reimport before wiring the animation names. Godot otherwise strips `_loop`, changing `idle_loop` to `idle` (and likewise for listen/think). This engine import option cannot be embedded in the GLB. Then instantiate it. Use the imported AnimationPlayer/AnimationTree to select and crossfade clips. Set `idle_loop`, `listen_loop`, `think_loop`, and `talk_base` to loop in Advanced Import Settings; keep `alert_snap` and `notify_perk` as one-shots. glTF itself does not encode playback loop flags.

Find a morph index by name on each MeshInstance3D using `find_blend_shape_by_name()`, then drive it with `set_blend_shape_value(index, value)`. Drive the two lower-claw morphs from audio amplitude in [0, 1]. Drive both eyes for blinking and both shell/spot morphs for a manual squash. Apply your cel shader to the six imported material regions.

Validated in Godot 4.7.2: clean import, exact bone/animation/morph names, all clips moving, matching loop endpoints, and runtime claw morph control while `talk_base` advances. Cel shader and Piper/audio integration remain the later runtime pass.

The ready-to-run test project is `godot_check/project.godot`; open it and press F5. Keys 1–6 select clips, Space opens/closes claws, and B blinks. It includes the required import settings and loop flags. `godot_check/godot_checks.json` records the checks.

To rerun: `godot --headless --path /home/panu/strawberry/godot_check --editor --import`, then `godot --headless --path /home/panu/strawberry/godot_check --script res://validate.gd`. After regenerating the GLB, copy the new file into `godot_check/` first.

## Regenerate

These steps replace the current Blender scene. Output directory is the `OUT` constant at the top of `build_strawberry.py`.

In Blender's Python console, run:

```python
exec(compile(open('/home/panu/strawberry/build_strawberry.py').read(), 'build_strawberry.py', 'exec'))
build(6)
```

Then, in a **separate console command or MCP call**, export and check:

```python
exec(compile(open('/home/panu/strawberry/export_validate.py').read(), 'export_validate.py', 'exec'))
```

Then check the exported file in a clean scene:

```python
exec(compile(open('/home/panu/strawberry/reimport_validate.py').read(), 'reimport_validate.py', 'exec'))
```

The separate export invocation lets Blender refresh dependencies after procedural Action/NLA creation. `build(n)` regenerates phases 1 through n. `run_phase(n)` reruns one phase if its prerequisites exist; after geometry/rig edits, regenerate dependent phases too. Each phase saves `strawberry_v1.blend` and front render `phase_NN.png` plus a JPEG preview. `check_morphs.py` separately checks every morph and saves full-value previews.

All six Actions use layered slots for the armature and two squash meshes. Each is pushed into identically named NLA tracks on its animated owners; export uses NLA_TRACKS to combine them into exactly six glTF clips. The source opens with `idle_loop` enabled. To preview another clip in Blender, load the build script definitions and call `preview('think_loop')`, then play the timeline.

## Evidence

- `export_checks.json`: exact GLB names, size, identity transforms, source playback checks over every frame, matching loop endpoints.
- `reimport.log`: clean Blender import, no warnings/errors.
- `reimport_checks.json`: one armature, exact bone hierarchy, 16 skinned meshes, rigid weights, live claw morph, motion in all six imported clips.
- `reimport_front.jpg` and `reimport_claw_open_L.jpg`: front renders of the actual exported model.
- `check_claws_open.jpg`, `check_blink_squash.jpg`, `clip_*.jpg`: morph and action previews.
- `roundtrip_check.blend`: intermediate import checkpoint, not the editable source.

Blender's glTF importer adds a hidden bone-display helper and may create helper empties plus suffixed mesh objects for animated skinned morphs. Those are importer internals; the GLB nodes and source object names retain the exact contract. Imported animation playback is verified through the imported Action slots because Blender's stashed NLA strips initially evaluate with zero influence.
