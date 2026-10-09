extends SceneTree
## Touch reactions (WIRING.md §13): tap against drag against hold from injected input events (mouse
## and touch), where she is touched (the ray hit test), each zone's reaction, escalation and its
## scuttle hook, a poke waking her, nothing over a line, a run or the step chip, the settings, and
## the "Talk when poked" message. Real widget, isolated settings, no daemon (a stand-in socket).
##
## godot --headless --path widget --script res://validate_touch.gd
class FakeWs:
	extends Node
	var sent: Array = []
	func is_open() -> bool:
		return true
	func send(data: Dictionary) -> void:
		sent.append(data)

class TestWidget:
	extends "res://widget.gd"
	func settings_path() -> String:
		return "user://touch_test_%s.cfg" % OS.get_process_id()
	func setup_ws() -> void:
		ws = FakeWs.new()
		add_child(ws)
	func setup_sleep() -> void:
		super.setup_sleep()
		sleeper.monitor_enabled = false

var failures: Array[String] = []
var report := {}
var widget: TestWidget
var touch: Node

func _initialize() -> void:
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error("FAIL: " + message)

func wait(seconds: float) -> void:
	await create_timer(seconds).timeout

## Model-space point (glTF: Blender x, z, -y) -> window pixel, through her current pose.
func aim(point: Vector3) -> Vector2:
	return touch.world_to_pixel(widget.model.global_transform * point)

func button(at: Vector2, pressed: bool) -> void:
	var e := InputEventMouseButton.new()
	e.button_index = MOUSE_BUTTON_LEFT
	e.pressed = pressed
	e.position = at
	e.global_position = at
	Input.parse_input_event(e)
	await process_frame

func motion(at: Vector2) -> void:
	var e := InputEventMouseMotion.new()
	e.position = at
	e.global_position = at
	Input.parse_input_event(e)
	Input.flush_buffered_events()
	await process_frame

func tap(at: Vector2, hold := 0.08) -> void:
	await button(at, true)
	await wait(hold)
	await button(at, false)

## A fresh start for the next poke: no streak, no recipe.
func calm() -> void:
	touch.streak = 0
	touch.last_poke_at = -100.0
	while touch.recipe != "" or widget.one_shot != "":
		await process_frame

func morph(mesh_name: String, key: String) -> float:
	var mesh: MeshInstance3D = widget.model.find_child(mesh_name, true, false)
	return mesh.get_blend_shape_value(mesh.find_blend_shape_by_name(key))

## The largest value `probe` returns over `seconds` of frames.
func peak_over(seconds: float, probe: Callable) -> float:
	var best := 0.0
	var waited := 0.0
	while waited < seconds:
		await process_frame
		waited += widget.get_process_delta_time()
		best = maxf(best, probe.call())
	return best

func run() -> void:
	widget = TestWidget.new()
	widget.place_at = Vector2.ZERO
	root.add_child(widget)
	touch = widget.touch
	await wait(0.3)
	widget.set_state("talking")   # talk_base holds still while the zones are aimed (no idle drift)
	await wait(1.6)
	check(widget.touch_reactions and not widget.touch_talk, "touch reactions on and talk off by default")

	# 1. Where she is touched: the ray meets her posed meshes.
	var spots := {
		"shell": [Vector3(0.0, 0.47, -0.12), "shell", 1.0],
		"belly": [Vector3(0.0, 0.31, -0.19), "belly", 1.0],
		"eye_L": [Vector3(0.16, 0.635, -0.12), "eye", 1.0],
		"eye_R": [Vector3(-0.16, 0.635, -0.12), "eye", -1.0],
		"claw_L": [Vector3(0.45, 0.47, -0.16), "claw", 1.0],
		"claw_R": [Vector3(-0.45, 0.47, -0.16), "claw", -1.0],
	}
	var pixels := {}
	var started := Time.get_ticks_usec()
	for name in spots:
		pixels[name] = aim(spots[name][0])
		var hit: Dictionary = touch.hit_test(pixels[name])
		report["hit_" + name] = [hit.zone, hit.side]
		check(hit.zone == spots[name][1] and hit.side == spots[name][2], "%s at %s should hit %s/%s, got %s/%s" % [name, pixels[name], spots[name][1], spots[name][2], hit.zone, hit.side])
	report["hit_test_ms"] = snappedf((Time.get_ticks_usec() - started) / 1000.0 / spots.size(), 0.1)
	var beside := Vector2(24, 540)
	check(touch.hit_test(beside).zone == "near", "a press beside her should be near, not on her")
	widget.set_state("idle")
	await wait(0.2)

	# 2. A quick, still press is a poke (the shell: a pat), and no drag.
	var shell: Vector2 = aim(spots.shell[0])
	await tap(shell)
	report["input_reaches_widget"] = touch.pokes == 1
	check(touch.pokes == 1 and touch.last_zone == "shell", "a tap on her shell should be one poke on the shell (pokes %d, zone %s)" % [touch.pokes, touch.last_zone])
	check(not widget.dragging and not widget.pressing, "a tap is not a drag")
	check(touch.recipe == "pat", "a pat on the shell, got %s" % touch.recipe)
	var squint := await peak_over(0.6, func(): return morph("mesh_eye_L", "squint"))
	report["pat_squint"] = snappedf(squint, 0.01)
	check(squint > 0.5, "a pat should make her squint contentedly, %.2f" % squint)
	await calm()

	# 3. A press that moves is a drag: no poke, the window follows (a no-op headless).
	var before: int = touch.pokes
	await button(shell, true)
	await motion(shell + Vector2(3, 0))
	check(not widget.dragging, "a few pixels of jitter is not a drag yet")
	await motion(shell + Vector2(30, 4))
	check(widget.dragging, "moving past the slop should start a drag")
	await button(shell + Vector2(30, 4), false)
	check(touch.pokes == before and not widget.dragging, "a drag is not a poke")
	# A slow still press (past TAP_S, short of HOLD_S) is neither.
	await tap(shell, 0.35)
	check(touch.pokes == before and touch.recipe == "", "a slow press is not a poke")

	# 4. A long press: she leans into it while it lasts, and eases out after.
	await button(shell, true)
	await wait(1.0)
	check(touch.recipe == "hold" and touch.hold_amount > 0.6, "a long press should be a hold (%s, %.2f)" % [touch.recipe, touch.hold_amount])
	await button(shell, false)
	check(touch.pokes == before, "a hold is not a poke")
	await wait(0.8)
	check(touch.hold_amount == 0.0 and touch.recipe == "", "the lean should ease out after the release")

	# 5. Each zone's reaction.
	await calm()
	await tap(aim(spots.belly[0]))
	check(touch.recipe == "tickle", "the belly should tickle, got %s" % touch.recipe)
	var squash := await peak_over(0.8, func(): return morph("mesh_shell", "squash"))
	report["tickle_squash"] = snappedf(squash, 0.01)
	check(squash > 0.3, "a tickle should squash her, %.2f" % squash)
	await calm()
	await tap(aim(spots.eye_L[0]))
	check(touch.recipe == "flinch", "an eye should flinch, got %s" % touch.recipe)
	var shut := await peak_over(0.4, func(): return morph("mesh_eye_L", "blink"))
	report["flinch_blink"] = snappedf(shut, 0.01)
	check(shut > 0.8, "a flinch should shut her eyes, %.2f" % shut)
	await calm()
	await tap(aim(spots.claw_R[0]))
	check(touch.recipe == "pinch", "a claw should pinch back, got %s" % touch.recipe)
	var pinch := await peak_over(1.0, func(): return widget.claw_controller.value(1))
	report["pinch_open"] = snappedf(pinch, 0.01)
	check(pinch > 0.4 and widget.claw_controller.value(0) < 0.01, "the touched claw (only) should snap, %.2f" % pinch)
	await calm()
	await wait(0.25)
	check(widget.claw_controller.value(1) < 0.01, "the claw should close after the pinch")
	await tap(aim(spots.claw_L[0]))
	check(touch.recipe == "claw_wave", "every other claw poke is a wave, got %s" % touch.recipe)
	await calm()

	# 6. Pokes in a row: curious, then annoyed (alert_snap), then the scuttle hook.
	var shots: int = widget.one_shots_played
	for i in 2:
		await tap(shell)
		await wait(0.15)
	check(touch.last_level == 1 and widget.one_shots_played == shots, "two pokes are still curious")
	await tap(shell)
	check(touch.last_level == 2 and widget.one_shots_played == shots + 1 and widget.one_shot == "alert_snap", "the third poke in a row should annoy her (alert_snap)")
	var scuttle_signals := [0]
	touch.scuttle_wanted.connect(func(): scuttle_signals[0] += 1)
	for i in 3:
		await wait(0.2)
		await tap(shell)
	report["streak"] = touch.streak
	check(touch.scuttles == 1 and scuttle_signals[0] == 1 and touch.last_level == 2, "the sixth should ask for the scuttle hook and stay annoyed")
	await calm()

	# 7. Nothing over a line, a run or the step chip: only a blink.
	widget.perform({"state": "talking", "text": "Hold on, I'm saying something.", "emotion": "neutral"})
	await wait(0.2)
	shots = widget.one_shots_played
	await tap(shell)
	check(touch.recipe == "noticed" and widget.one_shots_played == shots and widget.state == "talking", "a poke while she talks is only noticed")
	check(widget.claw_controller.value(0) < 0.01, "a poke while she talks leaves the claws alone")
	widget.bubble.hide()
	widget.bubble.speaking = false
	widget.set_state("idle")
	await calm()
	widget.step_chip.welcomed({"cancel": true})
	widget.step_chip.on_phase({"type": "thinking", "run_id": "r-1"})
	await tap(shell)
	check(touch.recipe == "noticed", "a poke during a run is only noticed, got %s" % touch.recipe)
	await calm()
	widget.step_chip.set_shown(true)
	await process_frame
	var chip: Rect2 = widget.step_chip.get_global_rect()
	before = touch.pokes
	await tap(chip.get_center())
	report["chip_rect"] = [chip.position, chip.size]
	check(touch.pokes == before and not widget.pressing, "a press on the step chip is the chip's, never a poke")
	widget.step_chip.on_phase({"type": "run.completed", "run_id": "r-1"})
	widget.step_chip.set_shown(false)
	widget.set_state("idle")
	await calm()

	# 8. Asleep: a poke wakes her gently, nothing else.
	widget.sleeper.begin_sleep()
	await wait(3.0)
	check(widget.sleeper.phase == "sleeping", "she should be asleep for the wake check")
	widget.sleeper.monitor_enabled = false
	touch.poke(shell)   # straight in: the widget's own _input already wakes her on any press
	check(widget.sleeper.phase == "waking" and touch.last_zone == "asleep", "a poke should wake her (%s)" % widget.sleeper.phase)
	await wait(2.0)
	check(widget.sleeper.phase == "awake", "awake again")
	await calm()

	# 9. Touch input: Godot turns a touch into the same mouse events.
	before = touch.pokes
	var finger := InputEventScreenTouch.new()
	finger.position = aim(spots.belly[0])
	finger.pressed = true
	Input.parse_input_event(finger)
	await process_frame
	await wait(0.06)
	finger = finger.duplicate()
	finger.pressed = false
	Input.parse_input_event(finger)
	await process_frame
	check(touch.pokes == before + 1 and touch.last_zone == "belly", "a finger tap should be a poke like a click")
	await calm()

	# 10. "Talk when poked": off sends nothing; on, the third in a row always asks the daemon.
	check(widget.ws.sent.is_empty(), "talk off: nothing should go to the daemon")
	widget.set_touch_talk(true)
	touch.talked_at = -100.0
	for i in 2:
		await tap(shell)   # these two ask now and then (TALK_CHANCE)
		await wait(0.15)
	touch.talked_at = -100.0
	await tap(shell)       # the poke that annoys her always asks, cooldown allowing
	var asked: Array = widget.ws.sent.filter(func(m): return m.get("type") == "poked")
	report["poked_sent"] = asked
	check(not asked.is_empty() and asked[-1].level == 2 and asked[-1].zone == "shell", "talk on: the annoyed poke should ask for a line")
	widget.set_touch_talk(false)
	await calm()

	# 11. The settings: off means no reaction (a tap is still not a drag); both persist; the menu.
	widget.set_touch_reactions(false)
	before = touch.pokes
	await tap(shell)
	check(touch.pokes == before and touch.recipe == "" and not widget.dragging, "touch reactions off: a tap does nothing")
	var cfg := ConfigFile.new()
	cfg.load(widget.settings_path())
	check(cfg.get_value("touch", "reactions", true) == false and cfg.get_value("touch", "talk", true) == false, "the touch settings should be saved")
	widget.menu._refresh()
	check(not widget.menu.is_item_checked(widget.menu.get_item_index(widget.menu.TOUCH_REACTIONS)), "the menu should show touch off")
	check(widget.menu.is_item_disabled(widget.menu.get_item_index(widget.menu.TOUCH_TALK)), "talk is greyed out while touch is off")
	widget.menu._on_pressed(widget.menu.TOUCH_REACTIONS)
	check(widget.touch_reactions, "the menu item should turn touch back on")

	report["played"] = touch.played
	var path: String = widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(path)
	report["passed"] = failures.is_empty()
	report["failures"] = failures
	print(JSON.stringify(report))
	print("touch checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
