# The 3D asset

`strawberry_v2.glb` is the crab the widget loads; the copy it runs from is `widget/strawberry_v2.glb`.
`strawberry_v2.blend` is the editable scene, and `build_strawberry.py` (with `eye_claw_geometry.py`)
is the procedural source it was built from: no textures, no external assets, no add-ons.

One armature, six bones (`root`, `body`, `eyestalk_L`, `eyestalk_R`, `claw_arm_L`, `claw_arm_R`), 17
meshes, six flat-colour materials, and seven clips (`idle_loop`, `listen_loop`, `think_loop`,
`talk_base`, `dance_loop`, `alert_snap`, `notify_perk`, plus `sleep_enter` / `sleep_loop` /
`wake_up`). Morphs: `blink`, `squint`, `eye_wide`, `claw_open_L`, `claw_open_R`, `squash`,
`leg_tuck` per leg. No clip animates the claw morphs: the clack is driven live from the audio
(WIRING.md §9 holds the contract the daemon and widget rely on).

## Easing

Each clip is keyed at every frame (30 fps) from a pose function in phase 6, and the keys are Bézier
with auto-clamped handles. The easing is in the pose functions: the loops are sine curves, the
one-shots ease in and out (`ease_in_out`), and the snappy move overshoots and settles
(`back_out`, about 10 %): `alert_snap` lifts the claws past their mark within 0.1 s and settles by
0.17 s. glTF samples the curves per frame, so Godot needs nothing new. The widget's procedural
layers use the same curves (`widget/easing.gd`).

## Rebuild

Headless, on the saved scene (reruns the clips, saves the scene, writes the GLB):

```bash
blender -b model/strawberry_v2.blend --python model/build_strawberry.py -- \
  --phase 6 --save --export widget/strawberry_v2.glb
godot --headless --path widget --editor --import
```

Or in Blender (5.2.2 LTS or later), from its Python console:

```python
exec(compile(open('~/strawberry/model/build_strawberry.py').read(), 'build_strawberry.py', 'exec'))
build(6)
export_glb('~/strawberry/widget/strawberry_v2.glb')
```

`build(n)` regenerates phases 1 through n; `run_phase(n)` reruns one when its prerequisites exist and
renders a checkpoint to `$STRAWBERRY_BUILD_OUT` (default: `strawberry-build` in the temp dir).
`export_glb` writes one animation per clip (its Action) with every channel at every frame, +Y up.
It clears the active actions first: with one left assigned (the last `preview`), the exporter
mixes its shape-key curves into every other clip. Re-exporting the scene this way reproduces the
checked-in GLB exactly. In Godot's Import dock, **Nodes → Use Name Suffixes** must stay off, or
`idle_loop` imports as `idle`; the checked-in `.glb.import` already has it off, along with the
loop flags.

`scripts/check_phase1.sh` is the end-to-end check that the widget still performs with a new asset.
