extends Label3D
## Speech bubble above the crab. Reveals the line over the speech duration, holds,
## fades, and reports `finished` so the widget can fall back to idle (WIRING.md §6).
## Duration comes from the audio when there is audio; otherwise from text length.

signal finished

const TINTS := {
	"neutral": Color("fff7ec"),
	"happy": Color("fff1a8"),
	"alert": Color("ffd9d0"),
	"angry": Color("ffb3a7"),
}
const INK := Color("201318")

var speaking := false
var line := ""
var tween: Tween

func _ready() -> void:
	billboard = BaseMaterial3D.BILLBOARD_ENABLED
	no_depth_test = true
	pixel_size = 0.0022
	font_size = 44
	outline_size = 18
	outline_modulate = INK
	modulate = TINTS.neutral
	autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	width = 540.0  # ~1.19 world units at this pixel_size; the window shows 1.25
	horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	vertical_alignment = VERTICAL_ALIGNMENT_BOTTOM
	text = ""
	visible = false

static func heuristic_duration(for_text: String) -> float:
	# Reading pace of roughly 18 characters a second, clamped so a one-word line still registers.
	return clampf(0.6 + for_text.length() * 0.055, 1.6, 9.0)

## Show `new_line`. `duration` <= 0 means "no audio, time it by length".
func speak(new_line: String, emotion := "neutral", duration := 0.0, hold := 0.9) -> void:
	if tween and tween.is_valid():
		tween.kill()
	line = new_line
	text = ""
	modulate = TINTS.get(emotion, TINTS.neutral)
	outline_modulate = INK
	visible = true
	speaking = true
	var reveal := duration if duration > 0.0 else heuristic_duration(line)
	tween = create_tween()
	tween.tween_method(_reveal, 0, line.length(), reveal)
	tween.tween_interval(hold)
	tween.tween_property(self, "modulate:a", 0.0, 0.35).set_ease(Tween.EASE_IN)
	tween.parallel().tween_property(self, "outline_modulate:a", 0.0, 0.35)
	tween.tween_callback(_done)

func _reveal(count: int) -> void:
	# Label3D has no visible_characters, so reveal by growing the string.
	text = line.left(count)

func _done() -> void:
	visible = false
	speaking = false
	text = ""
	finished.emit()
