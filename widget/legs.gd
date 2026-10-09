extends Node
## Her six legs (WIRING.md §13, "Legs"). Each has two bones (`leg_L1_upper`, `leg_L1_lower` … §9); the
## clips pose them, and this node layers the procedural leg moves on top: taps and stomps on the beat,
## the crouch and the landing of a hop, a tremble, tiptoes, a scrabble, settling into a hold, and the
## gait when her window moves.
##
## Everything is said as where a toe should go. Before the layers move anything (process priority
## 95, right after the clip) the node notes where the clip put each hip, knee and toe. Then the
## layers (reactions 150, dance styles 155, touch 157, the walk 140) add toe offsets and body lifts,
## and at 159, after them all, each leg is solved again: two-bone IK from the hip, wherever the
## body now is, to the clip's toe plus the offsets. So a toe the layers leave alone stays planted
## while the body pitches, rolls or crouches above it (the knee takes it up), and a toe asked to lift
## lifts from where the clip had it. With no offsets and the body where the clip left it, nothing is
## written and the clip's pose stands.
##
## Skeleton space (glTF): +X her left (the viewer's left), +Y up, +Z her back (she faces -Z).
## Legs are named L1..L3 and R1..R3, front to back.
##
## The gait is a tripod: L1, R2 and L3 step together, then R1, L2 and R3, half a cycle apart, the way
## the dance styles phase their moves. Its speed follows how fast her window moves (`motion`, set by
## wander.gd each frame from a drag or a walk), and she scuttles sideways: a crab walks to its side.

const Easing = preload("res://easing.gd")
const LEGS := ["L1", "L2", "L3", "R1", "R2", "R3"]
const TRIPOD_A := ["L1", "R2", "L3"]     # in phase; the other three half a cycle later
const STEP_HZ := 3.0                     # the cadence a walk aims for; strides grow past it
const MAX_HZ := 5.0
const STRIDE_PX := Vector2(8.0, 26.0)    # a stance's sweep, in window pixels, shortest to longest
const STEP_LIFT := 0.024                 # model units a swinging toe rises
const MOVING_PX_S := 12.0                # slower than this the window is standing still
const GAIT_IN_S := 0.12
const GAIT_OUT_S := 0.25                 # the feet settle back under her this fast

class Snapshot:
	extends Node
	var legs: Node
	func _process(_delta: float) -> void:
		legs.snapshot()

var widget: Node3D
var skeleton: Skeleton3D
var body_i := -1
var upper := {}                          # leg -> bone index
var lower := {}
var lengths := {}                        # leg -> Vector2(upper, lower)
var toe_rest := {}                       # leg -> the toe in skeleton space at rest
var rest_frames := {}                    # leg -> [upper frame, lower frame] at rest (see frame())
var hinges := {}                         # leg -> the knee's hinge axis at rest (the leg plane's normal)
var clip := {}                           # leg -> [hip, knee, toe, hinge] as the clip left them this frame
var clip_body := Transform3D()

var offsets := {}                        # leg -> Vector3, this frame's toe offsets (consumed at 159)
var body_lift := 0.0                     # this frame's body lift (negative: a crouch)
var motion := Vector2.ZERO               # her window's velocity, px/s (wander.gd)
var gait_phase := 0.0
var gait_weight := 0.0
var stride := 0.0                        # model units, signed along X
var footfalls := 0                       # tripods put down (the checks count them)
var solved_frames := 0
var units_per_px := 1.25 / 380.0

func setup(owner: Node3D, model: Node, camera: Camera3D) -> void:
	process_priority = 159
	widget = owner
	units_per_px = camera.size / float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	skeleton = model.find_children("*", "Skeleton3D", true, false)[0]
	body_i = skeleton.find_bone("body")
	for leg in LEGS:
		upper[leg] = skeleton.find_bone("leg_%s_upper" % leg)
		lower[leg] = skeleton.find_bone("leg_%s_lower" % leg)
		var hip := skeleton.get_bone_global_rest(upper[leg]).origin
		var knee := skeleton.get_bone_global_rest(lower[leg]).origin
		var toe := find_toe(model.find_child("mesh_leg_" + leg, true, false) as MeshInstance3D, leg, hip)
		toe_rest[leg] = toe
		lengths[leg] = Vector2(hip.distance_to(knee), knee.distance_to(toe))
		var n := (knee - hip).cross(toe - knee).normalized()
		hinges[leg] = n
		rest_frames[leg] = [frame(knee - hip, n), frame(toe - knee, n)]
	offsets = {}
	var pass_before := Snapshot.new()
	pass_before.legs = self
	pass_before.process_priority = 95
	add_child(pass_before)

## The toe at rest: the tip ring of the leg's tube, the vertices farthest from the hip (skeleton space).
func find_toe(mesh: MeshInstance3D, leg: String, hip: Vector3) -> Vector3:
	var bone: int = lower[leg]
	var to_skeleton := skeleton.get_bone_global_rest(bone)
	for b in mesh.skin.get_bind_count():
		if mesh.skin.get_bind_name(b) == skeleton.get_bone_name(bone) or mesh.skin.get_bind_bone(b) == bone:
			to_skeleton = to_skeleton * mesh.skin.get_bind_pose(b)
			break
	var points := PackedVector3Array()
	for s in mesh.mesh.get_surface_count():
		for v: Vector3 in mesh.mesh.surface_get_arrays(s)[Mesh.ARRAY_VERTEX]:
			points.append(to_skeleton * v)
	var far := 0.0
	for p in points:
		far = maxf(far, p.distance_to(hip))
	var sum := Vector3.ZERO
	var count := 0
	for p in points:
		if p.distance_to(hip) > far - 0.012:
			sum += p
			count += 1
	return sum / count

## A leg segment's frame: Y along the segment, X the knee's hinge (the leg plane's normal).
static func frame(along: Vector3, normal: Vector3) -> Basis:
	var y := along.normalized()
	var x := (normal - y * normal.dot(y)).normalized()
	return Basis(x, y, x.cross(y))

## Where the clip put each leg this frame, before any layer moves the body.
func snapshot() -> void:
	clip_body = skeleton.get_bone_global_pose(body_i)
	for leg in LEGS:
		var lo := skeleton.get_bone_global_pose(lower[leg])
		var up := skeleton.get_bone_global_pose(upper[leg])
		var toe := lo * (skeleton.get_bone_global_rest(lower[leg]).affine_inverse() * (toe_rest[leg] as Vector3))
		# The hinge turns with the upper bone; it stays good when the clip has the leg straight.
		var hinge := up.basis.orthonormalized() * skeleton.get_bone_global_rest(upper[leg]).basis.orthonormalized().inverse() * (hinges[leg] as Vector3)
		clip[leg] = [up.origin, lo.origin, toe, hinge]

# --- what the layers ask for ---------------------------------------------------------

## Move a toe by `offset` (skeleton space) this frame, from where the clip put it.
func add(leg: String, offset: Vector3) -> void:
	offsets[leg] = (offsets.get(leg, Vector3.ZERO) as Vector3) + offset

## The same offset for several legs of a side (`sign` +1: her left, the viewer's left). `inward`
## moves the toes toward her middle and is mirrored for each side.
func add_side(sign: float, offset: Vector3, numbers := [1, 2, 3], inward := 0.0) -> void:
	for n in numbers:
		add(("L" if sign > 0.0 else "R") + str(n), offset + Vector3(-sign * inward, 0.0, 0.0))

func add_all(offset: Vector3, inward := 0.0) -> void:
	add_side(1.0, offset, [1, 2, 3], inward)
	add_side(-1.0, offset, [1, 2, 3], inward)

## Raise the body (negative: lower it) this frame; planted toes bend their knees to stay down.
func lift(amount: float) -> void:
	body_lift += amount

## A tap or stomp over one beat (phase 0 on the beat): down on the floor at the beat, up between
## beats, and back down fast so it lands on the next one.
static func beat_lift(phase: float) -> float:
	var up := Easing.in_out((phase - 0.25) / 0.4)
	return up * (1.0 - pow(clampf((phase - 0.78) / 0.22, 0.0, 1.0), 2.0))

# --- the gait ------------------------------------------------------------------------

func advance_gait(delta: float) -> void:
	var speed := motion.length()
	var moving := speed > MOVING_PX_S
	gait_weight = move_toward(gait_weight, 1.0 if moving else 0.0, delta / (GAIT_IN_S if moving else GAIT_OUT_S))
	if gait_weight <= 0.0:
		gait_phase = 0.0
		stride = 0.0
		return
	if moving:
		# Strides sized for STEP_HZ, within STRIDE_PX; past the longest stride the cadence rises.
		var sweep_px := clampf(speed / (2.0 * STEP_HZ), STRIDE_PX.x, STRIDE_PX.y)
		var hz := minf(speed / (2.0 * sweep_px), MAX_HZ)
		var before := gait_phase
		gait_phase = fposmod(gait_phase + hz * delta, 1.0)
		if floori(before * 2.0) != floori(gait_phase * 2.0) or gait_phase < before:
			footfalls += 1
		# Window right (+x on screen) is her -X: the stride is signed along her X, sideways.
		var along := -motion.x / speed if speed > 0.0 else 0.0
		stride = along * sweep_px * units_per_px
	var w := Easing.in_out(gait_weight)
	for leg in LEGS:
		add(leg, step_offset(leg) * w)
	# A small dip as one tripod hands over to the other.
	lift(-0.004 * w * (0.5 + 0.5 * cos(TAU * 2.0 * gait_phase)))

## One toe's place in the cycle: in stance (the first half) it sweeps back under her as the body goes
## on, so it stays put on the desktop; in swing it lifts and reaches forward for the next stance.
func step_offset(leg: String) -> Vector3:
	var phase := fposmod(gait_phase + (0.0 if leg in TRIPOD_A else 0.5), 1.0)
	if phase < 0.5:
		return Vector3(stride * (0.5 - phase / 0.5), 0.0, 0.0)
	var u := (phase - 0.5) / 0.5
	return Vector3(stride * (Easing.in_out(u) - 0.5), STEP_LIFT * sin(PI * u), 0.0)

## Whether `leg` is on the floor in the gait now (stance), for the checks.
func in_stance(leg: String) -> bool:
	return fposmod(gait_phase + (0.0 if leg in TRIPOD_A else 0.5), 1.0) < 0.5

# --- the solve -----------------------------------------------------------------------

func _process(delta: float) -> void:
	advance_gait(delta)
	solve()
	offsets = {}
	body_lift = 0.0

func solve() -> void:
	if clip.is_empty():
		return
	if body_lift != 0.0:
		var parent := skeleton.get_bone_parent(body_i)
		var up := Vector3(0.0, body_lift, 0.0)
		if parent >= 0:
			up = skeleton.get_bone_global_pose(parent).basis.inverse() * up
		skeleton.set_bone_pose_position(body_i, skeleton.get_bone_pose_position(body_i) + up)
	var body := skeleton.get_bone_global_pose(body_i)
	if offsets.is_empty() and body.is_equal_approx(clip_body):
		return
	solved_frames += 1
	var turn := body.basis.orthonormalized() * clip_body.basis.orthonormalized().inverse()
	for leg in LEGS:
		var was: Array = clip[leg]
		var hip := skeleton.get_bone_global_pose(upper[leg]).origin
		var target: Vector3 = was[2] + (offsets.get(leg, Vector3.ZERO) as Vector3)
		var size: Vector2 = lengths[leg]
		var hinge: Vector3 = turn * (was[3] as Vector3)
		# The knee bends the way it does in the clip: across the hinge from the hip-to-toe line.
		var joints := reach(hip, target, size.x, size.y, (target - hip).cross(hinge))
		var knee: Vector3 = joints[0]
		var toe: Vector3 = joints[1]
		var normal := (knee - hip).cross(toe - knee)
		if normal.length() < 1e-6:
			normal = hinge
		elif normal.dot(hinge) < 0.0:
			normal = -normal
		var frames: Array = rest_frames[leg]
		var upper_global := frame(knee - hip, normal) * (frames[0] as Basis).inverse() * skeleton.get_bone_global_rest(upper[leg]).basis.orthonormalized()
		var lower_global := frame(toe - knee, normal) * (frames[1] as Basis).inverse() * skeleton.get_bone_global_rest(lower[leg]).basis.orthonormalized()
		skeleton.set_bone_pose_rotation(upper[leg], (body.basis.orthonormalized().inverse() * upper_global).get_rotation_quaternion())
		skeleton.set_bone_pose_rotation(lower[leg], (upper_global.inverse() * lower_global).get_rotation_quaternion())

## Two-bone IK: from `hip` toward `target`, segments `a` and `b` long, the knee bending toward `bend`.
## The reach is clamped (a target too far straightens the leg). Returns [knee, toe].
static func reach(hip: Vector3, target: Vector3, a: float, b: float, bend: Vector3) -> Array:
	var d := target - hip
	var dist := clampf(d.length(), absf(a - b) + 1e-4, a + b - 1e-4)
	var dir := d.normalized()
	var pole := bend - dir * bend.dot(dir)
	if pole.length() < 1e-6:
		pole = Vector3.UP - dir * dir.y
	pole = pole.normalized()
	var c := clampf((a * a + dist * dist - b * b) / (2.0 * a * dist), -1.0, 1.0)
	var knee := hip + (dir * c + pole * sqrt(1.0 - c * c)) * a
	return [knee, hip + dir * dist]

## Where a toe is now (skeleton space), for the checks and the hull.
func toe_now(leg: String) -> Vector3:
	var lo := skeleton.get_bone_global_pose(lower[leg])
	return lo * (skeleton.get_bone_global_rest(lower[leg]).affine_inverse() * (toe_rest[leg] as Vector3))
