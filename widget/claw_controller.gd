extends Node
## Procedural gestures leave the GLB's talking morphs available to live audio.
@export var minimum_think_pause := 5.0
@export var maximum_think_pause := 10.0
var held_open := false
var player: AnimationPlayer
var claws: Array[MeshInstance3D] = []
var indices: Array[int] = []
var rng := RandomNumberGenerator.new()
var mode := ""
var remaining := 0.0
var elapsed := -1.0
var gesture_duration := 1.2
var gesture_amount := 0.65
var side := 0
var gesture_count := 0
var owns_morphs := false

func setup(model: Node, animation_player: AnimationPlayer) -> void:
	process_priority = 100
	player = animation_player
	rng.randomize()
	side = rng.randi_range(0, 1)
	for suffix in ["L", "R"]:
		var claw := model.find_child("mesh_claw_lower_" + suffix, true, false) as MeshInstance3D
		claws.append(claw)
		indices.append(claw.find_blend_shape_by_name("claw_open_" + suffix))

func _process(delta: float) -> void:
	advance_gestures(delta)

func advance_gestures(delta: float) -> void:
	var clip := player.assigned_animation
	if clip != mode:
		mode = clip
		elapsed = -1.0
		remaining = rng.randf_range(minimum_think_pause, maximum_think_pause)
	var target := Vector2.ZERO
	var active := held_open or mode == "dance_loop" or mode == "think_loop" or mode == "alert_snap"
	if held_open:
		target = Vector2.ONE
	elif mode == "alert_snap":
		var t := player.current_animation_position / player.get_animation(mode).length
		target = Vector2.ONE * 0.35 * smoothstep(0.0, 0.08, t) * (1.0 - smoothstep(0.66, 1.0, t))
	elif mode == "dance_loop":
		# Same phase as the alternating leg lifts: four cycles per dance loop.
		var length := player.get_animation(mode).length
		var wave := sin(TAU * 4.0 * player.current_animation_position / length)
		target = Vector2(maxf(0.0, wave), maxf(0.0, -wave)) * 0.85
	elif mode == "think_loop":
		if elapsed < 0.0:
			remaining -= delta
			if remaining <= 0.0:
				elapsed = 0.0
				side = 1 - side
				gesture_duration = rng.randf_range(1.0, 1.4)
				gesture_amount = rng.randf_range(0.55, 0.8)
				gesture_count += 1
		if elapsed >= 0.0:
			elapsed += delta
			var t := elapsed / gesture_duration
			var amount := smoothstep(0.0, 0.3, t) * (1.0 - smoothstep(0.5, 1.0, t))
			target[side] = amount * gesture_amount
			if t >= 1.0:
				elapsed = -1.0
				remaining = rng.randf_range(minimum_think_pause, maximum_think_pause)
	if active:
		write_values(target)
		owns_morphs = true
	elif owns_morphs:
		# Clear our last gesture once, then stop writing (especially in talk_base).
		write_values(Vector2.ZERO)
		owns_morphs = false

func write_values(values: Vector2) -> void:
	for i in claws.size():
		claws[i].set_blend_shape_value(indices[i], values[i])
