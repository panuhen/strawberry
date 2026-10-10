extends SceneTree
## The dance's beat clock (WIRING.md §4c): estimates every 2 s at 128 BPM with ±30 ms of jitter on
## `next_beat`, an odd number of beats between some of them and an extra post 0.4 s after one (a
## section change). Sampled every frame, the moves never change faster than the curves themselves do
## on a perfect clock, the beat count steps one beat at a time, and she stays on the beat. With the bar
## group the count's parity is the bar's (even beats from the downbeat), reached by a slide when the
## bar comes late, never by a jump; a grid the tracker moves is eased over, too.
## Pure logic: dance_style.gd alone, simulated time, no window, no daemon.
##
## godot --headless --path widget --script res://validate_dance.gd
const DanceStyle = preload("res://dance_style.gd")

const BPM := 128.0
const PERIOD := 60.0 / BPM
const FPS := 60.0
const T0 := 1_800_000_000.0
const GRID := T0 - 0.3           # true beat 0; the first estimate's next beat is beat 1 (odd)
const LENGTH := 20.0
const JITTER := 0.03
const SLACK := 1.35              # the clock may run up to 25 % fast or slow while it eases a correction
const STYLES := ["rave", "headbang", "groove", "bounce"]
const KEYS := ["lift_l", "lift_r", "roll", "yaw", "pitch", "squash", "stomp_l", "stomp_r", "tap_l", "tap_r", "bar"]

var failures: Array[String] = []
var report := {}
var rng := RandomNumberGenerator.new()

func _initialize() -> void:
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error("FAIL: " + message)

## The watcher's estimate at `at`: the next true beat of the grid from `grid` (plus `jitter`), and the
## bar group when `bar_confidence` is given (four beats a bar, true beat 0 a downbeat).
func estimate(at: float, grid: float, jitter := 0.0, bar_confidence := -1.0) -> Dictionary:
	var n := ceili((at - grid) / PERIOD + 1e-9)
	var e := {"bpm": BPM, "period_s": PERIOD, "confidence": 0.8, "next_beat": grid + n * PERIOD + jitter,
		"evenness": 0.7, "low_ratio": 0.45, "density": 3.0, "loudness_db": -18.0, "steady": true}
	if bar_confidence >= 0.0:
		var index := posmod(n, 4)
		e["beats_per_bar"] = 4
		e["beat_index"] = index
		e["next_downbeat"] = e.next_beat + ((4 - index) % 4) * PERIOD
		e["downbeat_confidence"] = bar_confidence
	return e

## Posts every 2 s, and one 0.4 s after the post at 4 s. `grid_at` gives the true grid at a time,
## `bar_at` the bar confidence (negative: no bar group).
func posts(jitter: float, grid_at: Callable, bar_at: Callable) -> Array:
	var times := []
	for k in int(LENGTH / 2.0):
		times.append(T0 + 2.0 * k)
	times.insert(3, T0 + 4.4)
	var out := []
	for at in times:
		out.append([at, estimate(at, grid_at.call(at), rng.randf_range(-jitter, jitter), bar_at.call(at))])
	return out

## One style through simulated time at FPS, the posts applied as their times come.
func trace(style: String, sent: Array) -> Dictionary:
	var dance := DanceStyle.new()
	var out := {"t": [], "pos": []}
	for key in KEYS:
		out[key] = []
	var next := 0
	var frame := 0
	while frame <= LENGTH * FPS:
		var t := T0 + frame / FPS
		while next < sent.size() and sent[next][0] <= t:
			dance.set_tempo(sent[next][1], sent[next][0])
			next += 1
		var m: Dictionary = dance.moves(style, t)
		out.t.append(t)
		out.pos.append(dance.beat_position(t))
		for key in KEYS:
			out[key].append(dance.bar_phase(t) if key == "bar" else float(m[key]))
		frame += 1
	dance.free()
	return out

## The largest change from one frame to the next, per channel, apart for the frames where a new beat
## starts (a pump's attack on the beat is by design) and the frames between.
func steps(tr: Dictionary) -> Dictionary:
	var out := {}
	for key in KEYS:
		var between := 0.0
		var on_beat := 0.0
		for i in range(1, tr.t.size()):
			var d := absf(tr[key][i] - tr[key][i - 1])
			if key == "bar":
				d = minf(d, 1.0 - d)
			if floori(tr.pos[i]) != floori(tr.pos[i - 1]):
				on_beat = maxf(on_beat, d)
			else:
				between = maxf(between, d)
		out[key] = [between, on_beat]
	return out

## No jumps: every channel within what the same curve does on a perfect clock; the count one beat at a time.
func check_smooth(label: String, tr: Dictionary, reference: Dictionary) -> void:
	var got := steps(tr)
	var worst := 0.0
	for key in KEYS:
		for j in 2:
			var allowed: float = reference[key][j] * SLACK + 1e-4
			worst = maxf(worst, got[key][j] / maxf(reference[key][j], 1e-4))
			check(got[key][j] <= allowed, "%s: %s changed %.4f in a frame%s, a perfect clock %.4f" % [label, key, got[key][j], " on a beat" if j == 1 else "", reference[key][j]])
	report[label + "_worst_step_ratio"] = snappedf(worst, 0.01)
	var bad := 0
	for i in range(1, tr.t.size()):
		var d := floori(tr.pos[i]) - floori(tr.pos[i - 1])
		if d < 0 or d > 1:
			bad += 1
	check(bad == 0, "%s: the beat count should step one beat at a time (%d frames did not)" % [label, bad])

## The largest distance (in beats) from the true grid from `from` on.
func lock_error(tr: Dictionary, grid_at: Callable, from: float) -> float:
	var worst := 0.0
	for i in tr.t.size():
		if tr.t[i] < from:
			continue
		var d: float = tr.pos[i] - (tr.t[i] - grid_at.call(tr.t[i])) / PERIOD
		worst = maxf(worst, absf(d - roundf(d)))
	return worst

## Frames from `from` on where the count's parity is not the true beat's (even beats from the downbeat).
func off_bar(tr: Dictionary, from: float) -> int:
	var off := 0
	for i in tr.t.size():
		if tr.t[i] >= from and posmod(roundi(tr.pos[i] - (tr.t[i] - GRID) / PERIOD), 2) != 0:
			off += 1
	return off

func run() -> void:
	rng.seed = 128
	var fixed := func(_at: float) -> float: return GRID
	var no_bar := func(_at: float) -> float: return -1.0
	var references := {}
	for style in STYLES:
		references[style] = steps(trace(style, [[T0, estimate(T0, GRID)]]))
	# 0. The curves themselves: what swings or leans is as smooth on the beat as between beats (only
	# the pumps, nods, squashes and taps start on it). The rave's arms used to jump on every beat.
	var smooth := {"rave": ["lift_l", "lift_r", "roll", "yaw", "stomp_l", "stomp_r", "bar"],
		"groove": ["roll", "yaw", "bar"], "bounce": ["yaw", "bar"]}
	for style in smooth:
		for key in smooth[style]:
			var r: Array = references[style][key]
			check(r[1] <= r[0] * SLACK + 1e-4, "%s: %s should not jump on the beat (%.4f, between beats %.4f)" % [style, key, r[1], r[0]])

	# 1. Jitter, odd beat counts between posts, an extra post: no jumps, strict count, on the beat.
	var sent := posts(JITTER, fixed, no_bar)
	var odd := 0
	for i in range(1, sent.size()):
		if roundi((sent[i][1].next_beat - sent[i - 1][1].next_beat) / PERIOD) % 2 == 1:
			odd += 1
	report["odd_beat_gaps"] = odd
	check(odd >= 2, "the posts should have an odd number of beats between some of them")
	for style in STYLES:
		var tr := trace(style, sent)
		check_smooth("jitter_" + style, tr, references[style])
		var err := lock_error(tr, fixed, T0)
		report["jitter_%s_lock_beats" % style] = snappedf(err, 0.001)
		check(err < 0.12, "jitter %s: the clock should stay on the beat (off by %.3f beat)" % [style, err])
	# The claw pumps in groove go to alternate sides, one per beat.
	var groove := trace("groove", sent)
	var pumps := []
	for i in range(1, groove.t.size()):
		if floori(groove.pos[i]) != floori(groove.pos[i - 1]):
			pumps.append("L" if groove.lift_l[i] > groove.lift_r[i] else "R")
	var alternating := true
	for i in range(1, pumps.size()):
		alternating = alternating and pumps[i] != pumps[i - 1]
	report["groove_pumps"] = pumps.size()
	check(alternating and pumps.size() >= int(LENGTH / PERIOD) - 1, "groove's claw pumps should alternate sides beat by beat (%d pumps)" % pumps.size())

	# 2. The bar group from the start: the count's parity is the bar's at once.
	var with_bar := func(_at: float) -> float: return 0.64
	sent = posts(JITTER, fixed, with_bar)
	for style in ["rave", "groove"]:
		var tr := trace(style, sent)
		check_smooth("bar_" + style, tr, references[style])
		var off := off_bar(tr, T0)
		report["bar_%s_frames_off_bar" % style] = off
		check(off == 0, "bar %s: even beats should be the bar's from the first estimate (%d frames off)" % [style, off])

	# 3. The bar comes late (a guess at 0.3 first, ignored): the count starts on the wrong parity and
	# slides onto the bar over a few beats, without a jump.
	var late_bar := func(at: float) -> float: return -1.0 if at < T0 + 1.0 else (0.3 if at < T0 + 3.0 else 0.64)
	sent = posts(JITTER, fixed, late_bar)
	for style in ["rave", "groove"]:
		var tr := trace(style, sent)
		check_smooth("late_bar_" + style, tr, references[style])
		check(off_bar(tr, T0) > 0, "late bar %s: the test should start off the bar" % style)
		var aligned := 0.0
		for i in tr.t.size():
			if posmod(roundi(tr.pos[i] - (tr.t[i] - GRID) / PERIOD), 2) != 0:
				aligned = tr.t[i] + 1.0 / FPS - T0
		report["late_bar_%s_on_bar_after_s" % style] = snappedf(aligned, 0.01)
		check(aligned < 9.0, "late bar %s: the count should slide onto the bar within a few seconds (%.1f s)" % [style, aligned])

	# 4. The tracker moves its grid by 0.4 beat at 6 s: eased over, then on the new grid.
	var moved := func(at: float) -> float: return GRID if at < T0 + 6.0 else GRID + 0.4 * PERIOD
	sent = posts(0.0, moved, no_bar)
	for style in ["rave", "groove"]:
		var tr := trace(style, sent)
		check_smooth("moved_" + style, tr, references[style])
		var err := lock_error(tr, moved, T0 + 10.0)
		report["moved_%s_lock_beats" % style] = snappedf(err, 0.001)
		check(err < 0.05, "moved %s: the clock should follow the moved grid (off by %.3f beat)" % [style, err])

	report["passed"] = failures.is_empty()
	report["failures"] = failures
	print(JSON.stringify(report))
	print("dance clock checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
