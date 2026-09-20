extends Node
## Owns eyelid morphs independently of looping body animations.
@export var minimum_pause := 5.0
@export var maximum_pause := 10.0
@export var enabled := true
var held_closed := false
# Reaction recipes (reactions.gd) raise these; they are combined with the clip-driven values.
var extra_wide := 0.0
var extra_happy := 0.0
var extra_squint := 0.0
var eyes: Array[MeshInstance3D] = []
var indices: Array[int] = []
var wide_indices: Array[int] = []
var happy_indices: Array[int] = []
var squint_indices: Array[int] = []
var happy_values := Vector2.ZERO
var player: AnimationPlayer
var wide_value := 0.0
var previous_expression := ""
var rng := RandomNumberGenerator.new()
var remaining := 0.0
var elapsed := -1.0
var duration_scale := 1.0
var blink_count := 0
var value := 0.0

func setup(model: Node, animation_player: AnimationPlayer = null) -> void:
	player = animation_player
	process_priority = 100
	rng.randomize()
	eyes.clear()
	indices.clear()
	wide_indices.clear()
	happy_indices.clear()
	for side in ["L", "R"]:
		var eye := model.find_child("mesh_eye_" + side, true, false) as MeshInstance3D
		eyes.append(eye)
		indices.append(eye.find_blend_shape_by_name("blink"))
		wide_indices.append(eye.find_blend_shape_by_name("eye_wide"))
		happy_indices.append(eye.find_blend_shape_by_name("happy"))
		squint_indices.append(eye.find_blend_shape_by_name("squint"))
	remaining = rng.randf_range(minimum_pause, maximum_pause)

func _process(delta: float) -> void:
	advance_blink(delta)

func advance_blink(delta: float) -> void:
	value = 0.0
	wide_value = 0.0
	happy_values = Vector2.ZERO
	var clip: String = player.assigned_animation if is_instance_valid(player) else ""
	var reaction := ""
	if enabled and clip in ["notify_perk", "alert_snap"] and player.current_animation_position < player.get_animation(clip).length:
		reaction = clip
	var expression := "dance_loop" if enabled and clip == "dance_loop" else reaction
	if expression != previous_expression:
		elapsed = -1.0
		remaining = rng.randf_range(minimum_pause, maximum_pause)
		previous_expression = expression
	if held_closed:
		value = 1.0
	elif reaction == "alert_snap":
		var t := player.current_animation_position / player.get_animation(clip).length
		wide_value = smoothstep(0.0, 0.08, t) * (1.0 - smoothstep(0.66, 1.0, t))
	elif reaction == "notify_perk":
		var t := player.current_animation_position / player.get_animation("notify_perk").length
		# Anticipation squeeze, wide-eyed lift, then a soft landing squeeze.
		value = 0.72 * smoothstep(0.0, 0.11, t) * (1.0 - smoothstep(0.11, 0.22, t))
		wide_value = smoothstep(0.22, 0.34, t) * (1.0 - smoothstep(0.61, 0.86, t))
		value += 0.18 * smoothstep(0.86, 0.92, t) * (1.0 - smoothstep(0.92, 1.0, t))
	elif enabled and clip == "dance_loop":
		# A single held expression: no alternating squint or automatic dance blink.
		happy_values = Vector2.ONE
	elif enabled:
		if elapsed < 0.0:
			remaining -= delta
			if remaining <= 0.0:
				elapsed = 0.0
				duration_scale = rng.randf_range(0.95, 1.15)
				blink_count += 1
		if elapsed >= 0.0:
			elapsed += delta
			var t := elapsed / duration_scale
			if t < 0.18:
				value = smoothstep(0.0, 0.18, t)
			elif t < 0.25:
				value = 1.0
			elif t < 0.57:
				value = 1.0 - smoothstep(0.25, 0.57, t)
			else:
				elapsed = -1.0
				remaining = rng.randf_range(minimum_pause, maximum_pause)
	for i in eyes.size():
		eyes[i].set_blend_shape_value(indices[i], value)
		if wide_indices[i] >= 0:
			eyes[i].set_blend_shape_value(wide_indices[i], maxf(wide_value, extra_wide))
		if happy_indices[i] >= 0:
			eyes[i].set_blend_shape_value(happy_indices[i], maxf(happy_values[i], extra_happy))
		if squint_indices[i] >= 0:
			eyes[i].set_blend_shape_value(squint_indices[i], extra_squint)
