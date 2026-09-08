extends Node3D

const SCENE_PATH := "res://assets/scene.glb"
const MANIFEST_PATH := "res://assets/scene-manifest.json"

const WALK_SPEED := 2.3
const RUN_SPEED := 4.0
const MOUSE_SENSITIVITY := 0.0022
const MAX_PITCH := deg_to_rad(83.0)

const DEFAULT_CAMERA_POSITION := Vector3(0.77, 1.65, 0.0)
const DEFAULT_CAMERA_TARGET := Vector3(4.66, 0.92, -18.7)
const DEFAULT_CAMERA_FOV := 40.34
const DEFAULT_CAMERA_NEAR := 0.06
const DEFAULT_CAMERA_FAR := 350.0

var _player: Node3D
var _camera: Camera3D
var _rain: CPUParticles3D
var _status_label: Label
var _help_label: Label

var _reference_position := DEFAULT_CAMERA_POSITION
var _reference_target := DEFAULT_CAMERA_TARGET
var _walk_bounds_min := Vector3(0.20, 1.65, -18.6)
var _walk_bounds_max := Vector3(1.16, 1.65, 1.0)
var _fog_color := Color("#718793")
var _fog_near := 10.0
var _fog_far := 170.0
var _exit_light_position := Vector3(0.8, 3.10, -19.25)
var _step_light_positions: Array[Vector3] = [
	Vector3(0.24, 0.38, -7.8),
	Vector3(0.24, 0.38, -14.0),
]
var _scene_status := ""
var _camera_status := ""
var _runtime_status := ""


func _ready() -> void:
	_create_player_camera()
	_create_interface()
	_load_scene_manifest()
	_create_world_environment()
	_create_rain()
	_reset_reference_camera(false)
	_load_exported_scene()
	# The scene starts as a passive preview; only the explicit button captures the mouse.
	Input.set_mouse_mode(Input.MOUSE_MODE_VISIBLE)


func _process(delta: float) -> void:
	if Input.mouse_mode != Input.MOUSE_MODE_CAPTURED:
		return

	var movement := Vector3.ZERO
	var forward := -_player.global_transform.basis.z
	var right := _player.global_transform.basis.x
	forward.y = 0.0
	right.y = 0.0
	forward = forward.normalized()
	right = right.normalized()

	if Input.is_key_pressed(KEY_W) or Input.is_key_pressed(KEY_UP):
		movement += forward
	if Input.is_key_pressed(KEY_S) or Input.is_key_pressed(KEY_DOWN):
		movement -= forward
	if Input.is_key_pressed(KEY_D) or Input.is_key_pressed(KEY_RIGHT):
		movement += right
	if Input.is_key_pressed(KEY_A) or Input.is_key_pressed(KEY_LEFT):
		movement -= right

	if movement.length_squared() <= 0.0:
		return

	var speed := RUN_SPEED if Input.is_key_pressed(KEY_SHIFT) else WALK_SPEED
	_player.global_position += movement.normalized() * speed * delta
	_player.global_position.x = clampf(_player.global_position.x, _walk_bounds_min.x, _walk_bounds_max.x)
	_player.global_position.y = clampf(_reference_position.y, _walk_bounds_min.y, _walk_bounds_max.y)
	_player.global_position.z = clampf(_player.global_position.z, _walk_bounds_min.z, _walk_bounds_max.z)


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED:
		var mouse_event := event as InputEventMouseMotion
		_player.rotate_y(-mouse_event.relative.x * MOUSE_SENSITIVITY)
		_camera.rotate_x(-mouse_event.relative.y * MOUSE_SENSITIVITY)
		_camera.rotation.x = clampf(_camera.rotation.x, -MAX_PITCH, MAX_PITCH)
		get_viewport().set_input_as_handled()
		return

	if not (event is InputEventKey):
		return

	var key_event := event as InputEventKey
	if not key_event.pressed or key_event.echo:
		return

	match key_event.keycode:
		KEY_ESCAPE:
			_release_mouse()
		KEY_R:
			_reset_reference_camera()
		KEY_T:
			_toggle_rain()
		KEY_F1:
			_toggle_help()


func _create_world_environment() -> void:
	var environment := Environment.new()
	var sky := Sky.new()
	var sky_material := ProceduralSkyMaterial.new()
	sky_material.sky_top_color = Color("#243947")
	sky_material.sky_horizon_color = Color("#7d8d96")
	sky_material.ground_bottom_color = Color("#10191e")
	sky_material.ground_horizon_color = Color("#52616a")
	sky.sky_material = sky_material

	environment.background_mode = Environment.BG_SKY
	environment.sky = sky
	environment.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
	environment.ambient_light_color = Color("#93a8b3")
	environment.ambient_light_energy = 0.38
	environment.tonemap_mode = Environment.TONE_MAPPER_FILMIC
	environment.tonemap_exposure = 0.82
	environment.fog_enabled = true
	environment.fog_light_color = _fog_color
	environment.fog_light_energy = 0.52
	environment.fog_density = 0.008
	environment.fog_depth_begin = _fog_near
	environment.fog_depth_end = _fog_far
	environment.fog_depth_curve = 1.0
	environment.fog_sky_affect = 0.7

	var world_environment := WorldEnvironment.new()
	world_environment.name = "RainEnvironment"
	world_environment.environment = environment
	add_child(world_environment)

	var fill_light := DirectionalLight3D.new()
	fill_light.name = "SoftOvercastFill"
	fill_light.light_color = Color("#abc2cf")
	fill_light.light_energy = 0.32
	fill_light.rotation_degrees = Vector3(-42.0, -28.0, 0.0)
	fill_light.shadow_enabled = false
	add_child(fill_light)

	var exit_light := OmniLight3D.new()
	exit_light.name = "SubtleGreenExitLight"
	exit_light.position = _exit_light_position
	exit_light.light_color = Color("#a6d99b")
	exit_light.light_energy = 0.18
	exit_light.omni_range = 2.0
	exit_light.omni_attenuation = 2.5
	exit_light.shadow_enabled = false
	add_child(exit_light)

	for lamp_position in _step_light_positions:
		var step_lamp := OmniLight3D.new()
		step_lamp.name = "StepLamp"
		step_lamp.position = lamp_position
		step_lamp.light_color = Color("#d8dda8")
		step_lamp.light_energy = 0.09
		step_lamp.omni_range = 1.05
		step_lamp.omni_attenuation = 2.5
		step_lamp.shadow_enabled = false
		add_child(step_lamp)


func _create_player_camera() -> void:
	_player = Node3D.new()
	_player.name = "FirstPersonRig"
	add_child(_player)

	_camera = Camera3D.new()
	_camera.name = "PlayerCamera"
	_camera.current = true
	# KEEP_HEIGHT makes fov the requested vertical field of view.
	_camera.keep_aspect = Camera3D.KEEP_HEIGHT
	_camera.fov = DEFAULT_CAMERA_FOV
	_camera.near = DEFAULT_CAMERA_NEAR
	_camera.far = DEFAULT_CAMERA_FAR
	_player.add_child(_camera)


func _create_rain() -> void:
	_rain = CPUParticles3D.new()
	_rain.name = "ExteriorRain"
	_rain.position = Vector3(5.5, 8.6, -8.0)
	_rain.amount = 360
	_rain.lifetime = 1.55
	_rain.preprocess = 1.55
	_rain.local_coords = false
	_rain.emission_shape = CPUParticles3D.EMISSION_SHAPE_BOX
	# The box begins beyond x=2, keeping rain outside the covered corridor.
	_rain.emission_box_extents = Vector3(3.2, 0.15, 11.0)
	_rain.direction = Vector3(0.0, -1.0, 0.0)
	_rain.spread = 6.0
	_rain.gravity = Vector3(0.0, -3.0, 0.0)
	_rain.initial_velocity_min = 5.6
	_rain.initial_velocity_max = 7.1
	var rain_tint := Color(0.60, 0.70, 0.75, 0.325)
	_rain.color = rain_tint
	_rain.visibility_aabb = AABB(Vector3(-3.5, -14.0, -13.0), Vector3(7.0, 20.0, 26.0))

	var streak_mesh := QuadMesh.new()
	streak_mesh.size = Vector2(0.012, 0.42)
	var streak_material := StandardMaterial3D.new()
	streak_material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	streak_material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	streak_material.cull_mode = BaseMaterial3D.CULL_DISABLED
	streak_material.albedo_color = rain_tint
	streak_mesh.material = streak_material
	_rain.mesh = streak_mesh
	add_child(_rain)


func _create_interface() -> void:
	var canvas := CanvasLayer.new()
	canvas.name = "Interface"
	add_child(canvas)

	var viewport_ui := Control.new()
	viewport_ui.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	viewport_ui.mouse_filter = Control.MOUSE_FILTER_IGNORE
	canvas.add_child(viewport_ui)

	var panel := PanelContainer.new()
	panel.position = Vector2(20.0, 20.0)
	panel.size = Vector2(370.0, 270.0)
	panel.mouse_filter = Control.MOUSE_FILTER_STOP
	var panel_style := StyleBoxFlat.new()
	panel_style.bg_color = Color(0.025, 0.045, 0.055, 0.88)
	panel_style.border_color = Color(0.42, 0.62, 0.66, 0.46)
	panel_style.set_border_width_all(1)
	panel_style.set_corner_radius_all(8)
	panel_style.set_content_margin_all(14.0)
	panel.add_theme_stylebox_override("panel", panel_style)
	viewport_ui.add_child(panel)

	var content := VBoxContainer.new()
	content.add_theme_constant_override("separation", 9)
	panel.add_child(content)

	var title := Label.new()
	title.text = "RAIN GALLERY · GODOT"
	title.add_theme_font_size_override("font_size", 18)
	title.add_theme_color_override("font_color", Color("#d7eef0"))
	content.add_child(title)

	_help_label = Label.new()
	_help_label.text = "WASD / стрелки — движение\nМышь — обзор · Shift — быстрее\nR — исходная камера · T — дождь\nEsc — освободить курсор · F1 — скрыть подсказку"
	_help_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_help_label.add_theme_color_override("font_color", Color("#c0d0d6"))
	content.add_child(_help_label)

	var enter_button := Button.new()
	enter_button.text = "Войти в сцену"
	enter_button.custom_minimum_size = Vector2(0.0, 38.0)
	enter_button.pressed.connect(_enter_scene)
	content.add_child(enter_button)

	_status_label = Label.new()
	_status_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_status_label.add_theme_font_size_override("font_size", 12)
	_status_label.add_theme_color_override("font_color", Color("#aac6cc"))
	content.add_child(_status_label)


func _load_scene_manifest() -> void:
	if not FileAccess.file_exists(MANIFEST_PATH):
		_camera_status = "Камера и границы: scene-manifest.json не найден; использованы запасные значения."
		_update_status()
		return

	var json := JSON.new()
	var parse_error := json.parse(FileAccess.get_file_as_string(MANIFEST_PATH))
	if parse_error != OK:
		_camera_status = "Камера: ошибка JSON — %s" % json.get_error_message()
		_update_status()
		return

	var manifest = json.data
	if not (manifest is Dictionary):
		_camera_status = "Камера и границы: корень scene-manifest.json должен быть объектом."
		_update_status()
		return

	var coordinate_space := str(manifest.get("coordinate_space", "godot")).to_lower()
	if coordinate_space != "godot" and coordinate_space != "blender":
		coordinate_space = "godot"

	var camera_loaded := _load_camera_settings(manifest.get("camera", null), coordinate_space)
	var bounds_loaded := _load_walking_bounds(manifest.get("bounds", null), coordinate_space)
	_load_lighting_settings(manifest.get("lighting", null), coordinate_space)

	if camera_loaded and bounds_loaded:
		_camera_status = "Камера и границы загружены из scene-manifest.json (%s)." % coordinate_space
	elif camera_loaded:
		_camera_status = "Камера загружена; границы manifest имеют неверный формат."
	else:
		_camera_status = "Камера manifest имеет неверный формат; использована запасная точка."
	_update_status()


func _load_camera_settings(camera_data: Variant, coordinate_space: String) -> bool:
	if not (camera_data is Dictionary):
		return false

	var position_is_valid := _is_vector3(camera_data.get("position", null))
	var target_is_valid := _is_vector3(camera_data.get("target", null))
	_reference_position = _vector3_from_manifest(
		camera_data.get("position", null), DEFAULT_CAMERA_POSITION, coordinate_space
	)
	_reference_target = _vector3_from_manifest(
		camera_data.get("target", null), DEFAULT_CAMERA_TARGET, coordinate_space
	)

	var fov_value = camera_data.get("fov", _camera.fov)
	if _is_number(fov_value):
		_camera.fov = clampf(float(fov_value), 35.0, 110.0)
	var near_value = camera_data.get("near", _camera.near)
	if _is_number(near_value):
		_camera.near = clampf(float(near_value), 0.01, 5.0)
	var far_value = camera_data.get("far", _camera.far)
	if _is_number(far_value):
		_camera.far = maxf(float(far_value), _camera.near + 1.0)

	return position_is_valid and target_is_valid


func _load_walking_bounds(bounds_data: Variant, coordinate_space: String) -> bool:
	if not (bounds_data is Dictionary):
		return false
	var minimum_data = bounds_data.get("min", null)
	var maximum_data = bounds_data.get("max", null)
	if not _is_vector3(minimum_data) or not _is_vector3(maximum_data):
		return false

	var converted_minimum := _vector3_from_manifest(minimum_data, _walk_bounds_min, coordinate_space)
	var converted_maximum := _vector3_from_manifest(maximum_data, _walk_bounds_max, coordinate_space)
	var minimum := Vector3(
		minf(converted_minimum.x, converted_maximum.x),
		minf(converted_minimum.y, converted_maximum.y),
		minf(converted_minimum.z, converted_maximum.z)
	)
	var maximum := Vector3(
		maxf(converted_minimum.x, converted_maximum.x),
		maxf(converted_minimum.y, converted_maximum.y),
		maxf(converted_minimum.z, converted_maximum.z)
	)
	if minimum.x >= maximum.x or minimum.z >= maximum.z or minimum.y > maximum.y:
		return false

	_walk_bounds_min = minimum
	_walk_bounds_max = maximum
	return true


func _load_lighting_settings(lighting_data: Variant, coordinate_space: String) -> void:
	if not (lighting_data is Dictionary):
		return

	var fog_color_data = lighting_data.get("fogColor", null)
	if fog_color_data is String:
		_fog_color = Color.from_string(fog_color_data, _fog_color)
	var fog_near_data = lighting_data.get("fogNear", null)
	if _is_number(fog_near_data):
		_fog_near = maxf(float(fog_near_data), 0.0)
	var fog_far_data = lighting_data.get("fogFar", null)
	if _is_number(fog_far_data):
		_fog_far = maxf(float(fog_far_data), _fog_near + 1.0)

	var exit_light_data = lighting_data.get("exitLight", null)
	if _is_vector3(exit_light_data):
		_exit_light_position = _vector3_from_manifest(exit_light_data, _exit_light_position, coordinate_space)

	var step_lights_data = lighting_data.get("stepLights", null)
	if not (step_lights_data is Array):
		return
	var loaded_step_lights: Array[Vector3] = []
	for step_light_data in step_lights_data:
		if _is_vector3(step_light_data):
			loaded_step_lights.append(
				_vector3_from_manifest(step_light_data, Vector3.ZERO, coordinate_space)
			)
	if not loaded_step_lights.is_empty():
		_step_light_positions = loaded_step_lights


func _load_exported_scene() -> void:
	if not FileAccess.file_exists(SCENE_PATH):
		_scene_status = "⚠ scene.glb не найден. Положите экспорт в godot/assets/scene.glb и перезапустите."
		_update_status()
		return

	var document := GLTFDocument.new()
	var state := GLTFState.new()
	var import_error := document.append_from_file(SCENE_PATH, state)
	if import_error != OK:
		_scene_status = "⚠ Не удалось открыть scene.glb: %s" % error_string(import_error)
		_update_status()
		return

	var imported_scene := document.generate_scene(state)
	if imported_scene == null:
		_scene_status = "⚠ scene.glb прочитан, но Godot не создал сцену. Проверьте экспорт GLB."
		_update_status()
		return

	imported_scene.name = "ExportedGallery"
	add_child(imported_scene)
	# GLB may contain a reference camera; the controllable camera must remain active.
	_camera.make_current()
	_scene_status = "✓ scene.glb загружен в рантайме."
	_update_status()


func _enter_scene() -> void:
	Input.set_mouse_mode(Input.MOUSE_MODE_CAPTURED)
	_runtime_status = "Управление активно. Esc освобождает курсор."
	_update_status()


func _release_mouse() -> void:
	Input.set_mouse_mode(Input.MOUSE_MODE_VISIBLE)
	_runtime_status = "Курсор освобождён. Нажмите «Войти в сцену», чтобы продолжить."
	_update_status()


func _reset_reference_camera(show_status: bool = true) -> void:
	_player.global_position = _reference_position
	_player.rotation = Vector3.ZERO
	_camera.rotation = Vector3.ZERO

	var direction := _reference_target - _reference_position
	if direction.length_squared() < 0.0001:
		direction = Vector3(0.0, 0.0, -1.0)
	direction = direction.normalized()
	_player.rotation.y = atan2(-direction.x, -direction.z)
	_camera.rotation.x = asin(clampf(direction.y, -1.0, 1.0))
	_camera.make_current()

	if show_status:
		_runtime_status = "Камера возвращена к точке из manifest."
		_update_status()


func _toggle_rain() -> void:
	_rain.emitting = not _rain.emitting
	_runtime_status = "Дождь: %s." % ("включён" if _rain.emitting else "выключен")
	_update_status()


func _toggle_help() -> void:
	_help_label.visible = not _help_label.visible


func _update_status() -> void:
	if _status_label == null:
		return

	var message := _scene_status
	if not _camera_status.is_empty():
		message += "\n" if not message.is_empty() else ""
		message += _camera_status
	if not _runtime_status.is_empty():
		message += "\n" if not message.is_empty() else ""
		message += _runtime_status
	_status_label.text = message


func _is_number(value: Variant) -> bool:
	return typeof(value) == TYPE_INT or typeof(value) == TYPE_FLOAT


func _is_vector3(value: Variant) -> bool:
	if not (value is Array):
		return false
	var components: Array = value
	return components.size() == 3 and _is_number(components[0]) and _is_number(components[1]) and _is_number(components[2])


func _vector3_from_manifest(value: Variant, fallback: Vector3, coordinate_space: String) -> Vector3:
	if not _is_vector3(value):
		return fallback
	var components: Array = value
	var vector := Vector3(float(components[0]), float(components[1]), float(components[2]))
	if coordinate_space == "blender":
		return Vector3(vector.x, vector.z, -vector.y)
	return vector
