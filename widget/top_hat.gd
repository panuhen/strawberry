extends Node3D
## Optional procedural accessory, following the final body pose after dance/reaction layers.
var widget: Node3D
var skeleton: Skeleton3D
var shell: MeshInstance3D
var body_index := -1
var squash_index := -1
var rest_inverse := Transform3D.IDENTITY
var lift := 0.0
var tilt := 0.0

func setup(owner: Node3D, model: Node3D) -> void:
	widget = owner
	process_priority = 180
	skeleton = model.find_children("*", "Skeleton3D", true, false)[0]
	body_index = skeleton.find_bone("body")
	rest_inverse = skeleton.get_bone_global_rest(body_index).affine_inverse()
	shell = model.find_child("mesh_shell", true, false) as MeshInstance3D
	squash_index = shell.find_blend_shape_by_name("squash")
	var felt := StandardMaterial3D.new()
	felt.resource_name = "mat_hat_felt"
	felt.albedo_color = Color.html("28242f")
	felt.roughness = 1.0
	var brim := StandardMaterial3D.new()
	brim.resource_name = "mat_hat_brim"
	brim.albedo_color = Color.html("1d1a23")
	brim.roughness = 1.0
	var claw := model.find_child("mesh_claw_upper_L", true, false) as MeshInstance3D
	var band := claw.mesh.surface_get_material(0) as StandardMaterial3D
	cylinder("hat_brim", 0.119, 0.119, 0.014, 0.007, brim)
	cylinder("hat_crown", 0.073, 0.083, 0.15, 0.087, felt)
	cylinder("hat_band", 0.0835, 0.0845, 0.027, 0.033, band)
	update_pose(0.0)

func cylinder(part_name: String, top: float, bottom: float, height: float, y: float, material: Material) -> void:
	var geometry := CylinderMesh.new()
	geometry.top_radius = top
	geometry.bottom_radius = bottom
	geometry.height = height
	geometry.radial_segments = 40
	geometry.rings = 1
	geometry.material = material
	var part := MeshInstance3D.new()
	part.name = part_name
	part.mesh = geometry
	part.position.y = y
	add_child(part)

func _process(delta: float) -> void:
	if visible:
		update_pose(delta)

func update_pose(delta: float) -> void:
	var target_lift := 0.0
	var target_tilt := 0.0
	if widget.state == "dancing" and widget.player.assigned_animation == "dance_loop":
		var phase: float
		if widget.dance != null and widget.dance.active():
			phase = widget.dance.beat_phase(Time.get_unix_time_from_system())
		else:
			var beat_length: float = widget.player.get_animation("dance_loop").length / 8.0
			phase = fposmod(widget.player.current_animation_position / beat_length, 1.0)
		target_lift = 0.012 * pow(sin(PI * phase), 2.0)
		target_tilt = 0.035 * sin(TAU * phase)
	var blend := 1.0 - exp(-18.0 * delta)
	lift = lerpf(lift, target_lift, blend)
	tilt = lerpf(tilt, target_tilt, blend)
	var squash := shell.get_blend_shape_value(squash_index)
	# Same squash pivot/factor as the shell; base height follows the dome surface.
	var anchor := Transform3D(Basis(Vector3.BACK, tilt), Vector3(0, 0.52 - 0.0648 * squash + lift, 0.015))
	global_transform = skeleton.global_transform * skeleton.get_bone_global_pose(body_index) * rest_inverse * anchor
