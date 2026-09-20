extends Node3D
## The desktop widget: a frameless, transparent, always-on-top window that performs
## whatever strawberryd sends over the websocket (WIRING.md §1, §6, §13).
##
## Drag the crab to move her. Q quits, C cycles skins. Outlines are always on.
## Command-line (after `--`): --ws=ws://host:port/ws   --capture=/path/out.png

const CelStyle = preload("res://cel_style.gd")
const SkinPalettes = preload("res://skin_palettes.gd")
const WsClient = preload("res://ws_client.gd")
const Bubble = preload("res://bubble.gd")
const Badge = preload("res://badge.gd")
const Reactions = preload("res://reactions.gd")
const SpeechPlayer = preload("res://speech_player.gd")

# Must match the GLB and strawberryd/contract.py (WIRING.md §9).
const STATE_CLIPS := {
	"idle": "idle_loop",
	"listening": "listen_loop",
	"thinking": "think_loop",
	"talking": "talk_base",
	"dancing": "dance_loop",
}
const ONE_SHOTS := ["alert_snap", "notify_perk"]
const LOOPING := ["idle_loop", "listen_loop", "think_loop", "talk_base", "dance_loop"]
# States she settles back into after talking. listening/thinking are pipeline transients.
const PERSISTENT := ["idle", "dancing"]
const SETTINGS_PATH := "user://widget.cfg"
const PASSTHROUGH_PADDING := 18.0

var ws_url := "ws://127.0.0.1:8770/ws"
var capture_path := ""

var model: Node3D
var player: AnimationPlayer
var camera: Camera3D
var bubble: Label3D
var badge: Sprite3D
var reactions: Node
var speech: AudioStreamPlayer
var ws: Node
var blink_controller: Node
var claw_controller: Node
var pending_hops := 0
var one_shots_played := 0
var window_hops := 0

var skin_id := "strawberry"
var state := "idle"
var rest_state := "idle"
var one_shot := ""
var performances := 0
var dragging := false
var drag_offset := Vector2i.ZERO

func _ready() -> void:
	parse_args()
	setup_window()
	setup_scene()
	restore_settings()
	apply_appearance()
	setup_controllers()
	setup_reactions()
	setup_bubble()
	setup_ws()
	set_state("idle")
	call_deferred("update_passthrough")
	if capture_path != "":
		capture()

func parse_args() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--ws="):
			ws_url = arg.trim_prefix("--ws=")
		elif arg.begins_with("--capture="):
			capture_path = arg.trim_prefix("--capture=")

func is_headless() -> bool:
	return DisplayServer.get_name() == "headless"

# --- window shell -------------------------------------------------------------

func setup_window() -> void:
	# project.godot already asks for these; setting them again here keeps the four
	# properties that make this a widget in one readable place.
	var window := get_window()
	window.title = "Strawberry"
	window.borderless = true
	window.always_on_top = true
	window.transparent = true
	window.transparent_bg = true
	RenderingServer.set_default_clear_color(Color(0, 0, 0, 0))

## Only the crab's silhouette takes clicks; everything else falls through to the desktop.
func update_passthrough() -> void:
	if is_headless() or model == null:
		return
	var points := PackedVector2Array()
	for node in model.find_children("*", "MeshInstance3D", true, false):
		var mesh := node as MeshInstance3D
		var aabb: AABB = mesh.global_transform * mesh.get_aabb()
		for i in 8:
			points.append(camera.unproject_position(aabb.get_endpoint(i)))
	if points.size() < 3:
		return
	var hull := Geometry2D.convex_hull(points)
	var center := Vector2.ZERO
	for p in hull:
		center += p
	center /= hull.size()
	var padded := PackedVector2Array()
	for p in hull:
		padded.append(p + (p - center).normalized() * PASSTHROUGH_PADDING)
	get_window().mouse_passthrough_polygon = padded

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_LEFT:
		if event.pressed:
			dragging = true
			drag_offset = DisplayServer.mouse_get_position() - DisplayServer.window_get_position()
		elif dragging:
			dragging = false
			save_settings()
	elif event is InputEventMouseMotion and dragging:
		DisplayServer.window_set_position(DisplayServer.mouse_get_position() - drag_offset)

func _unhandled_key_input(event: InputEvent) -> void:
	if not event is InputEventKey or not event.pressed or event.echo:
		return
	match event.keycode:
		KEY_Q:
			get_tree().quit()
		KEY_C:
			cycle_skin()

# --- scene --------------------------------------------------------------------

func setup_scene() -> void:
	model = load("res://strawberry_v2.glb").instantiate()
	add_child(model)
	player = model.find_child("AnimationPlayer", true, false) as AnimationPlayer
	player.animation_finished.connect(_on_animation_finished)
	for clip_name in LOOPING:
		player.get_animation(clip_name).loop_mode = Animation.LOOP_LINEAR
	for clip_name in ONE_SHOTS:
		player.get_animation(clip_name).loop_mode = Animation.LOOP_NONE

	camera = Camera3D.new()
	add_child(camera)
	camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	camera.keep_aspect = Camera3D.KEEP_WIDTH
	camera.size = 1.25
	# Crab in the lower part of the window, room for a five-line bubble above (view spans y -0.14..1.70).
	camera.position = Vector3(0, 0.78, -3)
	camera.look_at(Vector3(0, 0.78, 0), Vector3.UP)
	camera.current = true

	var light := DirectionalLight3D.new()
	add_child(light)
	light.position = Vector3(-1, 3, -2)
	light.look_at(Vector3(0, 0.3, 0), Vector3.UP)
	light.light_energy = 1.0

	var world := WorldEnvironment.new()
	world.environment = Environment.new()
	world.environment.background_mode = Environment.BG_COLOR
	world.environment.background_color = Color(0, 0, 0, 0)
	world.environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	world.environment.ambient_light_color = Color.WHITE
	world.environment.ambient_light_energy = 0.65
	add_child(world)

func setup_controllers() -> void:
	blink_controller = preload("res://blink_controller.gd").new()
	blink_controller.setup(model, player)
	add_child(blink_controller)
	claw_controller = preload("res://claw_controller.gd").new()
	claw_controller.setup(model, player)
	add_child(claw_controller)

func setup_reactions() -> void:
	reactions = Reactions.new()
	add_child(reactions)
	reactions.setup(model, blink_controller)
	speech = SpeechPlayer.new()
	add_child(speech)
	speech.setup(model)

func setup_bubble() -> void:
	bubble = Bubble.new()
	bubble.position = Vector3(0, 0.98, 0)
	add_child(bubble)
	bubble.finished.connect(_on_speech_finished)
	# Everything from the anchor to the top edge of the view, minus a small margin.
	bubble.max_height = view_top() - bubble.position.y - 0.04
	badge = Badge.new()
	# Between the eyes and the bubble's bottom line, viewer's left (+X with this camera),
	# so the text can grow upward as far as it likes without running into it.
	badge.position = Vector3(0.5, 0.86, 0)
	add_child(badge)

## World-space y of the top edge of the orthographic view (KEEP_WIDTH: height follows the aspect).
func view_top() -> float:
	var w := float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var h := float(ProjectSettings.get_setting("display/window/size/viewport_height"))
	return camera.position.y + camera.size * h / w / 2.0

## The window itself jumps: a real X11 window bouncing on the desktop, not a sprite in a box.
func hop_window() -> void:
	if is_headless() or dragging:
		return
	window_hops += 1
	var base := DisplayServer.window_get_position()
	var tween := create_tween()
	tween.tween_method(func(y: float): DisplayServer.window_set_position(Vector2i(base.x, base.y - int(y))), 0.0, 26.0, 0.14).set_ease(Tween.EASE_OUT).set_trans(Tween.TRANS_QUAD)
	tween.tween_method(func(y: float): DisplayServer.window_set_position(Vector2i(base.x, base.y - int(y))), 26.0, 0.0, 0.16).set_ease(Tween.EASE_IN).set_trans(Tween.TRANS_QUAD)
	tween.tween_method(func(y: float): DisplayServer.window_set_position(Vector2i(base.x, base.y - int(y))), 0.0, 7.0, 0.08).set_ease(Tween.EASE_OUT)
	tween.tween_method(func(y: float): DisplayServer.window_set_position(Vector2i(base.x, base.y - int(y))), 7.0, 0.0, 0.09).set_ease(Tween.EASE_IN)
	tween.tween_callback(func(): DisplayServer.window_set_position(base))

func setup_ws() -> void:
	ws = WsClient.new()
	ws.url = ws_url
	add_child(ws)
	ws.message_received.connect(_on_message)
	ws.connected.connect(func(): print("strawberryd connected: ", ws_url))
	ws.disconnected.connect(func(): print("strawberryd disconnected; reconnecting"))

func apply_appearance() -> void:
	# Cel shading and the ink outline are part of her look, not options.
	CelStyle.apply(model, true, true, SkinPalettes.colors(skin_id))

func cycle_skin() -> void:
	var index: int = SkinPalettes.ORDER.find(skin_id)
	skin_id = SkinPalettes.ORDER[(index + 1) % SkinPalettes.ORDER.size()]
	apply_appearance()
	save_settings()

# --- performing (the receiving end of the contract) -----------------------------

func _on_message(data: Dictionary) -> void:
	if data.has("command"):
		run_command(data)
		return
	if not data.has("state"):
		push_warning("ignoring message without state: " + JSON.stringify(data))
		return
	perform(data)

func perform(data: Dictionary) -> void:
	performances += 1
	var new_state := str(data.get("state", "idle"))
	if not STATE_CLIPS.has(new_state):
		push_warning("unknown state %s; using idle" % new_state)
		new_state = "idle"
	var emotion := str(data.get("emotion", "neutral"))
	set_state(new_state)

	var anim := str(data.get("anim", ""))
	if anim != "":
		if anim in ONE_SHOTS:
			play_one_shot(anim)
		else:
			push_warning("unknown anim %s; ignored" % anim)

	var reaction := str(data.get("reaction", ""))
	if reaction != "":
		if reaction == "double_hop":
			play_one_shot("notify_perk")
			pending_hops = 1
		reactions.play(reaction)
	if bool(data.get("hop", false)):
		hop_window()

	var text := str(data.get("text", "")).strip_edges()
	var audio := str(data.get("audio", ""))
	# With audio the bubble is timed to the wav; without it, to the text length (§6).
	var duration := 0.0
	if audio != "":
		duration = speech.play_file(audio)
	elif speech.playing:
		speech.stop()

	var icon := str(data.get("icon", ""))
	if icon != "" and text != "":
		badge.show_icon(icon)
	else:
		badge.hide_icon()

	if text != "":
		bubble.speak(text, emotion, duration)
	elif duration > 0.0:
		# Audio with nothing to show: hold the talking pose until the sound ends.
		var timer := get_tree().create_timer(duration + 0.3)
		timer.timeout.connect(func(): if state == "talking" and not bubble.speaking: set_state(rest_state))
	elif new_state == "talking":
		# Talking with nothing to say: don't mouth forever.
		var timer := get_tree().create_timer(1.0)
		timer.timeout.connect(func(): if state == "talking" and not bubble.speaking: set_state(rest_state))

func run_command(data: Dictionary) -> void:
	match str(data.get("command", "")):
		"quit":
			get_tree().quit()
		"skin":
			var id := str(data.get("value", ""))
			if SkinPalettes.SKINS.has(id):
				skin_id = id
				apply_appearance()
				save_settings()
		_:
			push_warning("unknown command: " + JSON.stringify(data))

func set_state(new_state: String) -> void:
	state = new_state
	if new_state in PERSISTENT:
		rest_state = new_state
	if one_shot == "":
		play_clip(STATE_CLIPS[state])

func play_clip(clip_name: String, blend := 0.2) -> void:
	if player.assigned_animation != clip_name or not player.is_playing():
		player.play(clip_name, blend)

func play_one_shot(clip_name: String) -> void:
	one_shot = clip_name
	one_shots_played += 1
	player.play(clip_name, 0.1)

func _on_animation_finished(clip_name: StringName) -> void:
	if String(clip_name) == one_shot:
		if pending_hops > 0:
			pending_hops -= 1
			play_one_shot(one_shot)
			return
		one_shot = ""
		play_clip(STATE_CLIPS[state])

func _on_speech_finished() -> void:
	# The daemon never sends a follow-up; speech ending returns her to what she was
	# doing: dancing if music is on, otherwise idle (§1).
	badge.hide_icon()
	if state == "talking":
		set_state(rest_state)

# --- settings -------------------------------------------------------------------

func restore_settings() -> void:
	var config := ConfigFile.new()
	var have := config.load(SETTINGS_PATH) == OK
	if have:
		var saved_skin: Variant = config.get_value("appearance", "skin", "strawberry")
		if saved_skin is String and SkinPalettes.SKINS.has(saved_skin):
			skin_id = saved_skin
	if is_headless():
		return
	# Best effort: X11 honours this, a Wayland compositor may not (WIRING.md §13).
	var usable := DisplayServer.screen_get_usable_rect()
	var size := DisplayServer.window_get_size()
	if usable.size.x <= 0 or usable.size.y <= 0:
		return
	var target: Vector2i
	if have and config.has_section_key("window", "x"):
		target = Vector2i(int(config.get_value("window", "x")), int(config.get_value("window", "y")))
	else:
		target = usable.position + usable.size - size - Vector2i(24, 24)
	target.x = clampi(target.x, usable.position.x, usable.position.x + usable.size.x - size.x)
	target.y = clampi(target.y, usable.position.y, usable.position.y + usable.size.y - size.y)
	DisplayServer.window_set_position(target)

func save_settings() -> void:
	var config := ConfigFile.new()
	config.load(SETTINGS_PATH)
	config.set_value("appearance", "skin", skin_id)
	if not is_headless():
		var pos := DisplayServer.window_get_position()
		config.set_value("window", "x", pos.x)
		config.set_value("window", "y", pos.y)
	var err := config.save(SETTINGS_PATH)
	if err != OK:
		push_warning("could not save widget settings: " + error_string(err))

# --- evidence -------------------------------------------------------------------

func capture() -> void:
	# Evidence shot: a message performance so the wave, the badge and the bubble are in frame.
	await get_tree().create_timer(0.4).timeout
	var icon := ProjectSettings.globalize_path("res://capture_phase1.png")
	perform({"state": "talking", "reaction": "wave", "icon": icon, "emotion": "happy",
		"text": "James wants to know if you are still on for tonight, and whether you remembered the cake!"})
	await get_tree().create_timer(3.6).timeout
	await RenderingServer.frame_post_draw
	var image := get_viewport().get_texture().get_image()
	var err := image.save_png(capture_path)
	print("capture %s -> %s" % [capture_path, error_string(err)])
	get_tree().quit()
