extends SceneTree
## Isolated accessory checks: no websocket and no writes to the user's widget.cfg.
class TestWidget:
	extends "res://widget.gd"
	var test_settings := "user://hat_test.cfg"
	func settings_path() -> String:
		return test_settings
	func setup_ws() -> void:
		pass

var failures: Array[String] = []
var capture_dir := ""

func _initialize() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--capture-dir="):
			capture_dir = arg.trim_prefix("--capture-dir=")
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error(message)

func capture(name: String) -> void:
	if capture_dir == "":
		return
	await process_frame
	await process_frame
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(capture_dir.path_join(name + ".png"))

func run() -> void:
	var widget := TestWidget.new()
	widget.test_settings = "user://hat_test_%s.cfg" % OS.get_process_id()
	widget.look_at = Vector2(190, 340)
	root.add_child(widget)
	await process_frame
	check(widget.model.find_child("mesh_belly", true, false) != null, "Real widget must include the belly")
	check(not widget.top_hat.visible and not widget.top_hat_enabled, "Hat should be opt-in")
	widget.menu._on_pressed(widget.menu.TOP_HAT)
	widget.menu._refresh()
	check(widget.top_hat.visible and widget.menu.is_item_checked(widget.menu.get_item_index(widget.menu.TOP_HAT)), "Hat menu toggle failed")
	var cfg := ConfigFile.new()
	check(cfg.load(widget.settings_path()) == OK and cfg.get_value("appearance", "top_hat", false), "Hat preference not persisted")
	var ids := {}
	for i in widget.menu.item_count:
		if not widget.menu.is_item_separator(i):
			check(not ids.has(widget.menu.get_item_id(i)), "Duplicate menu item id")
			ids[widget.menu.get_item_id(i)] = true
	check(widget.menu.get_item_index(widget.menu.RESTART_WIDGET) >= 0, "Missing restart widget control")
	widget.set_skin("mint")
	var band := widget.top_hat.get_node("hat_band") as MeshInstance3D
	var material := band.get_surface_override_material(0) as ShaderMaterial
	check(material != null and material.get_shader_parameter("base_color").is_equal_approx(widget.SkinPalettes.colors("mint")["mat_claw"]), "Hat band should follow skin palette")
	widget.set_skin("strawberry")
	widget.blink_controller.enabled = false
	widget.player.play("idle_loop", 0.0)
	widget.player.advance(0.0)
	widget.player.pause()
	widget.top_hat.update_pose(0.0)
	await capture("hat_idle")
	var hat := widget.top_hat
	widget.set_state("dancing")
	widget.player.play("dance_loop", 0.0)
	var max_lift := 0.0
	var max_tilt := 0.0
	for frame in range(240):
		widget.player.advance(1.0 / 60.0)
		hat.update_pose(1.0 / 60.0)
		max_lift = maxf(max_lift, hat.lift)
		max_tilt = maxf(max_tilt, absf(hat.tilt))
		check(hat.global_position.is_finite(), "Hat transform must remain finite")
	check(max_lift > 0.009 and max_lift <= 0.0121, "Dance bounce should be small but visible")
	check(max_tilt > 0.015 and max_tilt < 0.036, "Dance hat tilt out of bounds")
	widget.player.seek(0.24, true)
	widget.player.pause()
	hat.update_pose(0.2)
	await capture("hat_dance")
	widget.set_state("idle")
	widget.player.advance(0.0)
	for i in range(90):
		hat.update_pose(1.0 / 60.0)
	check(hat.lift < 0.0001 and absf(hat.tilt) < 0.0001, "Hat should settle after dancing")
	# A full squash lowers the contact point by the same amount as the shell surface.
	hat.shell.set_blend_shape_value(hat.squash_index, 1.0)
	hat.update_pose(0.0)
	var attachment: Transform3D = hat.skeleton.global_transform * hat.skeleton.get_bone_global_pose(hat.body_index) * hat.rest_inverse
	var contact: Vector3 = attachment.affine_inverse() * hat.global_position
	check(absf(contact.y - 0.4552) < 0.0002, "Hat detached from squashed shell")
	widget.menu._on_pressed(widget.menu.TOP_HAT)
	check(not widget.top_hat.visible, "Hat should hide cleanly")
	cfg.load(widget.settings_path())
	check(not cfg.get_value("appearance", "top_hat", true), "Disabled hat preference should persist")
	var test_file := widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(test_file)
	var report := {"passed": failures.is_empty(), "failures": failures, "belly_in_widget": true,
		"max_dance_lift": max_lift, "max_tilt_radians": max_tilt, "menu_and_persistence": true}
	FileAccess.open("res://hat_checks.json", FileAccess.WRITE).store_string(JSON.stringify(report, "\t"))
	print(JSON.stringify(report))
	quit(0 if failures.is_empty() else 1)
