# Top hat and model updates

Right-click Strawberry and toggle **Top hat**. It starts off and the preference is saved in `user://widget.cfg` under `[appearance] top_hat`. The black felt hat has a band that follows the skin's claw colour. It is built procedurally by `top_hat.gd`, follows the body's final pose and shell squash, and adds a gentle lift and tilt during dancing. Beat-driven dance styles use the current beat phase; the ordinary dance uses its animation clock. The accessory is separate from the GLB.

Right-click **Restart widget** to reload the widget and its model while keeping its saved preferences and the daemon running. **Apply settings (restart daemon)** is for daemon configuration and does not reload the model.

For a widget that was launched before the Restart widget menu existed: right-click **Quit** (or press Q), then run:

```sh
/home/panu/strawberry/bin/strawberry widget
```

The live widget loads `widget/strawberry_v2.glb`; Blender iterations are exported to `v2/strawberry_v2.glb`. After a new export, copy that file into `widget/` and run `godot --headless --path /home/panu/strawberry/widget --editor --import` before restarting. The current widget copy includes the lighter belly.

`validate_hat.gd` checks the menu, preference persistence, palette band, bounded dance bounce, squash attachment and belly model in an isolated widget with no daemon connection. Headless: `godot --headless --path widget --script res://validate_hat.gd`. Add `-- --capture-dir=/absolute/folder` in a graphical run to capture idle and dance poses.
