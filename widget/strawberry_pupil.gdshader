shader_type spatial;
render_mode specular_disabled, ambient_light_disabled;

// The cel shader plus a vertex stage that slides the pupil over the eyeball (WIRING.md §13).
// The pupil is a small ellipsoid baked into the eye mesh's ink surface; the eye mesh is bound
// rigidly to one bone, so `to_rest` (authored rest space <- posed skeleton space) recovers the
// authored position of every vertex exactly, and the ones near `pupil_center` get rotated
// about `eye_center` by the gaze angles before going back through `from_rest`.

uniform vec4 base_color : source_color = vec4(0.13, 0.07, 0.09, 1.0);
uniform vec4 shadow_tint : source_color = vec4(0.78, 0.71, 0.75, 1.0);
uniform float shadow_edge : hint_range(-1.0, 1.0) = 0.15;
uniform float highlight_edge : hint_range(-1.0, 1.0) = 0.68;
uniform float edge_softness : hint_range(0.001, 0.15) = 0.025;

uniform mat4 to_rest = mat4(1.0);
uniform mat4 from_rest = mat4(1.0);
uniform vec3 eye_center = vec3(0.16, 0.635, -0.12);
uniform vec3 pupil_center = vec3(0.16, 0.639, -0.158);
uniform float pupil_radius = 0.034;
uniform vec2 gaze = vec2(0.0);  // yaw about +Y, pitch about +X, radians; the pupil rests on -Z

void vertex() {
    vec3 rest = (to_rest * vec4(VERTEX, 1.0)).xyz;
    if (distance(rest, pupil_center) < pupil_radius) {
        float cy = cos(gaze.x);
        float sy = sin(gaze.x);
        float cp = cos(gaze.y);
        float sp = sin(gaze.y);
        mat3 ry = mat3(vec3(cy, 0.0, -sy), vec3(0.0, 1.0, 0.0), vec3(sy, 0.0, cy));
        mat3 rx = mat3(vec3(1.0, 0.0, 0.0), vec3(0.0, cp, sp), vec3(0.0, -sp, cp));
        mat3 turn = ry * rx;
        vec3 moved = eye_center + turn * (rest - eye_center);
        VERTEX = (from_rest * vec4(moved, 1.0)).xyz;
        NORMAL = normalize(mat3(from_rest) * (turn * (mat3(to_rest) * NORMAL)));
    }
}

void fragment() {
    ALBEDO = base_color.rgb;
    ROUGHNESS = 1.0;
}

void light() {
    float facing = dot(normalize(NORMAL), normalize(LIGHT));
    float mid = smoothstep(shadow_edge - edge_softness, shadow_edge + edge_softness, facing);
    float high = smoothstep(highlight_edge - edge_softness, highlight_edge + edge_softness, facing);
    vec3 shade = mix(shadow_tint.rgb, vec3(0.72), mid);
    shade = mix(shade, vec3(1.0), high);
    DIFFUSE_LIGHT += ALBEDO * shade * LIGHT_COLOR * ATTENUATION / 3.14159265;
}
