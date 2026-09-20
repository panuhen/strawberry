extends AudioStreamPlayer
## Her voice, heard (WIRING.md §6). Plays the daemon's wav through a dedicated bus carrying a
## spectrum analyser, and each frame turns the loudness of the speech band into how far the
## claws open. The wav has to play through Godot for that: the analyser only hears this bus.
##
## Runs after the reaction recipes (priority 150) so a wave mid-sentence still clacks.

signal speech_finished

const BUS_NAME := "Speech"
const LOW_HZ := 90.0
const HIGH_HZ := 4000.0
const FLOOR_DB := -52.0   # quieter than this is a closed claw
const CEIL_DB := -16.0    # louder than this is fully open
const MAX_OPEN := 0.9
const ATTACK := 34.0      # per-second rates for the envelope follower
const RELEASE := 14.0

var analyzer: AudioEffectSpectrumAnalyzerInstance
var claws: Array[MeshInstance3D] = []
var claw_indices: Array[int] = []
var level := 0.0          # 0..1 mouth openness this frame
var peak_level := 0.0     # highest level in the current line, for the acceptance check
var wrote_claws := false
var lines_played := 0
var load_failures := 0

func setup(model: Node) -> void:
	process_priority = 160
	var index := AudioServer.get_bus_index(BUS_NAME)
	if index == -1:
		index = AudioServer.bus_count
		AudioServer.add_bus(index)
		AudioServer.set_bus_name(index, BUS_NAME)
		var effect := AudioEffectSpectrumAnalyzer.new()
		effect.buffer_length = 0.25
		effect.fft_size = AudioEffectSpectrumAnalyzer.FFT_SIZE_1024
		AudioServer.add_bus_effect(index, effect)
	bus = BUS_NAME
	analyzer = AudioServer.get_bus_effect_instance(index, 0) as AudioEffectSpectrumAnalyzerInstance
	for suffix in ["L", "R"]:
		var claw := model.find_child("mesh_claw_lower_" + suffix, true, false) as MeshInstance3D
		if claw:
			claws.append(claw)
			claw_indices.append(claw.find_blend_shape_by_name("claw_open_" + suffix))
	finished.connect(_on_finished)

## Start the wav at `path`. Returns its length in seconds, or 0.0 if it could not be loaded
## (the caller then times the bubble by text length, and she performs silently).
func play_file(path: String) -> float:
	var stream := AudioStreamWAV.load_from_file(path)
	if stream == null:
		load_failures += 1
		push_warning("could not load speech wav: " + path)
		return 0.0
	self.stream = stream
	peak_level = 0.0
	lines_played += 1
	play()
	return stream.get_length()

func _process(delta: float) -> void:
	if playing and analyzer:
		var magnitude := analyzer.get_magnitude_for_frequency_range(LOW_HZ, HIGH_HZ).length()
		var db := linear_to_db(maxf(magnitude, 1e-6))
		var target := clampf((db - FLOOR_DB) / (CEIL_DB - FLOOR_DB), 0.0, 1.0)
		var rate := ATTACK if target > level else RELEASE
		level = lerpf(level, target, 1.0 - exp(-rate * delta))
		peak_level = maxf(peak_level, level)
		write_claws(level * MAX_OPEN)
		wrote_claws = true
	elif wrote_claws:
		level = 0.0
		write_claws(0.0)
		wrote_claws = false

func write_claws(amount: float) -> void:
	for i in claws.size():
		claws[i].set_blend_shape_value(claw_indices[i], amount)

## Current claw_open value on the left claw; the acceptance check reads this.
func claw_value() -> float:
	if claws.is_empty():
		return 0.0
	return claws[0].get_blend_shape_value(claw_indices[0])

func _on_finished() -> void:
	speech_finished.emit()
