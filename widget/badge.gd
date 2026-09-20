extends Sprite3D
## The notifying app's icon, shown beside the bubble while she talks (WIRING.md §13).
## PNG/JPG load directly; SVG is rasterised by Godot at load.

const HEIGHT := 0.19  # world units

var loaded_path := ""

func _ready() -> void:
	billboard = BaseMaterial3D.BILLBOARD_ENABLED
	no_depth_test = true
	texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR_WITH_MIPMAPS
	visible = false

func show_icon(path: String) -> bool:
	var image := load_image(path)
	if image == null:
		visible = false
		return false
	texture = ImageTexture.create_from_image(image)
	pixel_size = HEIGHT / maxf(1.0, float(image.get_height()))
	loaded_path = path
	visible = true
	return true

func hide_icon() -> void:
	visible = false

static func load_image(path: String) -> Image:
	if not FileAccess.file_exists(path):
		return null
	var image := Image.new()
	var err: Error
	if path.get_extension().to_lower() == "svg":
		var bytes := FileAccess.get_file_as_bytes(path)
		err = image.load_svg_from_buffer(bytes, 4.0)  # rasterise large, we scale down
	else:
		err = image.load(path)
	if err != OK or image.is_empty():
		push_warning("badge: could not load %s (%s)" % [path, error_string(err)])
		return null
	return image
