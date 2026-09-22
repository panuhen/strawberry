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

## Rebuild

In Blender (5.2.2 LTS or later), from its Python console:

```python
exec(compile(open('~/strawberry/model/build_strawberry.py').read(), 'build_strawberry.py', 'exec'))
build(6)
```

`build(n)` regenerates phases 1 through n; `run_phase(n)` reruns one when its prerequisites exist.
Export to glTF with +Y up, then copy the result over `widget/strawberry_v2.glb` and reimport the
Godot project. In Godot's Import dock, **Nodes → Use Name Suffixes** must stay off, or `idle_loop`
imports as `idle`; the checked-in `.glb.import` already has it off, along with the loop flags.

`scripts/check_phase1.sh` is the end-to-end check that the widget still performs with a new asset.
