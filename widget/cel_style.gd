extends RefCounted

const CEL = preload("res://strawberry_cel.gdshader")
const OUTLINE = preload("res://strawberry_outline.gdshader")

static func apply(model: Node, enabled: bool = true, outlines: bool = true, palette: Dictionary = {}) -> void:
	var materials: Dictionary = {}
	var outline := ShaderMaterial.new()
	outline.shader = OUTLINE
	for node in model.find_children("*", "MeshInstance3D", true, false):
		var mesh := node as MeshInstance3D
		for surface in mesh.mesh.get_surface_count():
			var source := mesh.mesh.surface_get_material(surface) as StandardMaterial3D
			if not enabled or source == null:
				mesh.set_surface_override_material(surface, null)
				continue
			var name := source.resource_name
			if not materials.has(name):
				var cel := ShaderMaterial.new()
				cel.resource_name = name
				cel.shader = CEL
				cel.set_shader_parameter("base_color", palette.get(name, source.albedo_color.linear_to_srgb()))
				# Keep the cream shell pattern clean; ink already outlines the pupils.
				if outlines and name != "mat_cream" and name != "mat_ink":
					cel.next_pass = outline
				materials[name] = cel
			mesh.set_surface_override_material(surface, materials[name])
