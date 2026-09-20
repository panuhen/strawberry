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
const BASE_FONT_SIZE := 44
const MIN_FONT_SIZE := 28

## World-unit height available above the anchor; the widget sets it from the camera view.
var max_height := 0.7
var speaking := false
var line := ""
var tween: Tween

func _ready() -> void:
	billboard = BaseMaterial3D.BILLBOARD_ENABLED
	no_depth_test = true
	pixel_size = 0.0022
	font_size = BASE_FONT_SIZE
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
	fit_font(new_line)
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

## Lay the text out the way Label3D will (same font, width and word wrapping) and shrink
## the font until the block fits in max_height. A long line reads better small than cut off.
func fit_font(for_text: String) -> void:
	font_size = BASE_FONT_SIZE
	while font_size > MIN_FONT_SIZE and measured_height(for_text, font_size) > max_height:
		font_size -= 2

## Height in world units of the wrapped text at `size`, outline included.
func measured_height(for_text: String, size: int) -> float:
	var paragraph := layout(for_text, size)
	return (paragraph.get_size().y + outline_size) * pixel_size

func estimate_lines(for_text: String, size: int) -> int:
	return layout(for_text, size).get_line_count()

func layout(for_text: String, size: int) -> TextParagraph:
	var paragraph := TextParagraph.new()
	paragraph.width = width
	paragraph.break_flags = TextServer.BREAK_MANDATORY | TextServer.BREAK_WORD_BOUND | TextServer.BREAK_ADAPTIVE
	paragraph.add_string(for_text, font if font != null else ThemeDB.fallback_font, size)
	return paragraph

func _reveal(count: int) -> void:
	# Label3D has no visible_characters, so reveal by growing the string.
	text = line.left(count)

func _done() -> void:
	visible = false
	speaking = false
	text = ""
	finished.emit()
