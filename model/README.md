# The 3D asset

`strawberry_v2.glb` is the crab the widget loads; the copy it runs from is `widget/strawberry_v2.glb`.
`strawberry_v2.blend` is the editable scene, and `build_strawberry.py` (with `eye_claw_geometry.py`)
is the procedural source it was built from: no textures, no external assets, no add-ons.

One armature, 20 bones: `root`, `body`, `eyestalk_L/R`, `claw_arm_L/R`, a pincer per lower claw
(`pincer_L/R`, under the claw arm) and two per leg (`leg_L1_upper`, `leg_L1_lower` … `leg_R3_lower`;
legs numbered front to back, the upper under `body`, the lower under its upper). 17 meshes, six
flat-colour materials, and ten clips (`idle_loop`, `listen_loop`, `think_loop`, `talk_base`,
`dance_loop`, `alert_snap`, `notify_perk`, `sleep_enter`, `sleep_loop`, `wake_up`). Morphs: `blink`,
`squint`, `eye_wide`, `happy`, `squash`. No clip animates the pincers: the clack is driven live from
the audio (WIRING.md §9 holds the contract the daemon and widget rely on).

## The rig

Every mesh but the legs follows one bone with weight 1 (`rigid_check` asserts it, posed). The
pincer's head is the lower claw's `hinge` and its local X is the front axis, so opening is a turn
about X; at 48° it gives exactly the shape the old `claw_open` key did (asserted too). A leg is one
tube from root to toe around a rounded knee (`curved_leg`): rings up to the bend follow the upper
bone, rings past it the lower, and the weight passes between them over `KNEE_BLEND` with smoothstep
weights, so a bent knee stays round under the cel shading and the outline. Each leg bone's local X
is the leg plane's normal, the knee's hinge.

The clips pose the legs by where each toe should be (`pose_leg`: two-bone IK in the body's rest
frame, the knee bending the way it bends at rest, the reach clamped). The targets reproduce the old
keys: `tuck_toe` pulls a toe toward its root as `leg_tuck` did (`notify_perk`'s hop,
`dance_loop`'s alternating lifts), and `fold_toe` (asleep) splays the toes out and down beside the
lowered body. A toe at rest leaves both bones at identity, so the rest pose is the mesh as built.

## Easing

Each clip is keyed at every frame (30 fps) from a pose function in phase 6, and the keys are Bézier
with auto-clamped handles. The easing is in the pose functions: the loops are sine curves, the
one-shots ease in and out (`ease_in_out`), and the snappy move overshoots and settles
(`back_out`, about 10 %): `alert_snap` lifts the claws past their mark within 0.1 s and settles by
0.17 s. glTF samples the curves per frame, so Godot needs nothing new. The widget's procedural
layers use the same curves (`widget/easing.gd`).

## Rebuild

Headless, on the saved scene (rebuilds the rig, the morphs and the clips, saves the scene, writes the
GLB; `--phase 6` reruns only the clips; `--factory-startup` keeps the user's add-ons out of it):

```bash
blender -b --factory-startup model/strawberry_v2.blend --python model/build_strawberry.py -- \
  --phase 4 --save --export widget/strawberry_v2.glb
godot --headless --path widget --editor --import
```

Or in Blender (5.2.2 LTS or later), from its Python console:

```python
exec(compile(open('~/strawberry/model/build_strawberry.py').read(), 'build_strawberry.py', 'exec'))
build(6)
export_glb('~/strawberry/widget/strawberry_v2.glb')
```

`build(n)` regenerates phases 1 through n; `run_phase(n)` reruns one when its prerequisites exist and
saves and renders a checkpoint to `$STRAWBERRY_BUILD_OUT`, by default your own
`$XDG_CACHE_HOME/strawberry-build` (`~/.cache/strawberry-build`), created private (0700). An existing
one that is a symlink, not a directory or not yours is refused; set `STRAWBERRY_BUILD_OUT` then.
`export_glb` writes one animation per clip (its Action) with every channel at every frame, +Y up.
It clears the active actions first: with one left assigned (the last `preview`), the exporter
mixes its shape-key curves into every other clip. Re-exporting the scene this way reproduces the
checked-in GLB exactly. In Godot's Import dock, **Nodes → Use Name Suffixes** must stay off, or
`idle_loop` imports as `idle`; the checked-in `.glb.import` already has it off, along with the
loop flags.

`scripts/check_phase1.sh` is the end-to-end check that the widget still performs with a new asset.
