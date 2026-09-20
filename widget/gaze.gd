extends Node
## Her pupils follow the mouse (WIRING.md §13). Each frame the cursor's screen position is
## unprojected into the scene at the eyes' depth, the direction from each eyeball centre to it
## becomes a yaw and pitch, and the pupil shader slides the pupil over the eyeball by that much.
## After the cursor has sat still for a while she looks straight ahead again.
##
## The pupils are baked into the eye meshes (surface 1, the ink material), so no bone is needed:
## strawberry_pupil.gdshader moves just those vertices, given the bone's pose each frame.

const PupilShader = preload("res://strawberry_pupil.gdshader")
const MAX_ANGLE := 0.7           # radians either way; the pupil slides ~2/3 of the eyeball radius
const LOOK_DEPTH := 2.5          # world units (1.25 = window width); sets how far away saturates
const FOLLOW_RATE := 14.0        # per second, envelope toward the target
const RELAX_AFTER := 12.0        # seconds of a still cursor before she looks ahead
const EYES := {"L": 1.0, "R": -1.0}

var camera: Camera3D
var skeleton: Skeleton3D
var window: Window
var meshes: Dictionary = {}       # side -> MeshInstance3D
var materials: Dictionary = {}    # side -> ShaderMaterial
var bones: Dictionary = {}        # side -> bone index
var rests: Dictionary = {}        # side -> Transform3D global rest of the bone
var gaze := Vector2.ZERO          # current (yaw, pitch), shared by both eyes
var target := Vector2.ZERO
var last_mouse := Vector2i(-1, -1)
var still_for := 0.0
var look_override := Vector2(-1, -1)  # viewport pixel to look at; (-1,-1) = the real cursor
var enabled := true
var frames := 0

func setup(model: Node, cam: Camera3D) -> void:
	process_priority = 170
	camera = cam
	window = get_window()
	skeleton = model.find_child("*", true, false) as Skeleton3D
	if skeleton == null:
		skeleton = model.find_children("*", "Skeleton3D", true, false)[0]
	for side in EYES:
		var mesh := model.find_child("mesh_eye_" + side, true, false) as MeshInstance3D
		var bone := skeleton.find_bone("eyestalk_" + side)
		if mesh == null or bone < 0:
			push_warning("gaze: eye %s not found" % side)
			continue
		meshes[side] = mesh
		bones[side] = bone
		rests[side] = skeleton.get_bone_global_rest(bone)
		var material := ShaderMaterial.new()
		material.shader = PupilShader
		var s: float = EYES[side]
		# Authored positions (Blender x, y, z -> glTF x, z, -y), from v2/build_strawberry.py.
		material.set_shader_parameter("eye_center", Vector3(s * 0.16, 0.635, -0.12))
		material.set_shader_parameter("pupil_center", Vector3(s * 0.16, 0.639, -0.158))
		materials[side] = material
	apply_materials()

## Put the pupil material on the ink surface of both eyes. CelStyle.apply() replaces surface
## materials whenever the skin changes, so the widget calls this again afterwards.
func apply_materials() -> void:
	for side in meshes:
		var mesh: MeshInstance3D = meshes[side]
		var ink := mesh.get_surface_override_material(1) as ShaderMaterial
		if ink and ink.get_shader_parameter("base_color") != null:
			materials[side].set_shader_parameter("base_color", ink.get_shader_parameter("base_color"))
		mesh.set_surface_override_material(1, materials[side])

## Eyeball centre in world space for `side`, following the posed bone.
func eye_world(side: String) -> Vector3:
	var pose: Transform3D = skeleton.get_bone_global_pose(bones[side])
	var rest: Transform3D = rests[side]
	var rest_center: Vector3 = materials[side].get_shader_parameter("eye_center")
	return skeleton.global_transform * (pose * (rest.affine_inverse() * rest_center))

## Where the cursor is in this window's viewport pixels, even when it is outside the window.
func cursor_in_viewport() -> Vector2:
	if look_override.x >= 0.0:
		return look_override
	return Vector2(DisplayServer.mouse_get_position() - DisplayServer.window_get_position())

## Viewport pixel -> world point on the crab's plane. Done from the project's window size and
## the orthographic camera rather than Camera3D.project_position, which needs a real viewport
## (headless runs have none the right size). KEEP_WIDTH: camera.size spans the window width,
## and the camera looks from -Z, so +X is the viewer's left.
func pixel_to_world(pixel: Vector2, z: float) -> Vector3:
	var w := float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var h := float(ProjectSettings.get_setting("display/window/size/viewport_height"))
	var units_per_px := camera.size / w
	var origin := camera.global_position
	return Vector3(origin.x - (pixel.x - w / 2.0) * units_per_px, origin.y - (pixel.y - h / 2.0) * units_per_px, z)

func target_for(pixel: Vector2) -> Vector2:
	# Both eyes share one gaze; aim from the midpoint between them so they stay parallel.
	var mid := (eye_world("L") + eye_world("R")) * 0.5
	var point := pixel_to_world(pixel, mid.z)
	var dir := point - mid
	# Pretend the cursor floats LOOK_DEPTH in front of her: near her the pupils barely move,
	# a few hundred pixels away they are fully to the side.
	dir.z = -LOOK_DEPTH
	dir = dir.normalized()
	# The pupil rests on -Z. Ry(a) sends -Z to (-sin a, 0, -cos a); Rx(b) sends it to (0, sin b, -cos b).
	var yaw := -atan2(dir.x, -dir.z)
	var pitch := atan2(dir.y, -dir.z)
	return Vector2(clampf(yaw, -MAX_ANGLE, MAX_ANGLE), clampf(pitch, -MAX_ANGLE, MAX_ANGLE))

func _process(delta: float) -> void:
	if meshes.is_empty():
		return
	frames += 1
	if enabled and (look_override.x >= 0.0 or DisplayServer.get_name() != "headless"):
		var mouse := Vector2i(cursor_in_viewport())
		if mouse != last_mouse:
			last_mouse = mouse
			still_for = 0.0
		else:
			still_for += delta
		target = Vector2.ZERO if still_for > RELAX_AFTER else target_for(Vector2(mouse))
	else:
		target = Vector2.ZERO
	gaze = gaze.lerp(target, 1.0 - exp(-FOLLOW_RATE * delta))
	for side in meshes:
		var pose: Transform3D = skeleton.get_bone_global_pose(bones[side])
		var to_rest: Transform3D = rests[side] * pose.affine_inverse()
		var material: ShaderMaterial = materials[side]
		material.set_shader_parameter("to_rest", to_rest)
		material.set_shader_parameter("from_rest", to_rest.affine_inverse())
		material.set_shader_parameter("gaze", gaze)
