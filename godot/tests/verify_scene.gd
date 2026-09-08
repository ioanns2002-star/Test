extends SceneTree


func _initialize() -> void:
	call_deferred("_verify")


func _require(condition: bool, label: String) -> bool:
	if not condition:
		push_error(label)
		quit(1)
	return condition


func _verify() -> void:
	var packed := load("res://main.tscn") as PackedScene
	if not _require(packed != null, "Main scene must load"):
		return
	var app := packed.instantiate()
	root.add_child(app)
	await process_frame
	var imported := app.get_node_or_null("ExportedGallery")
	if not _require(imported != null, "GLB must actually import, not just show an error panel"):
		return
	if not _require(imported.find_children("*", "MeshInstance3D", true, false).size() >= 30, "Imported geometry is missing"):
		return
	var manifest: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://assets/scene-manifest.json"))
	var position: Array = manifest.camera.position
	var target: Array = manifest.camera.target
	var expected := Vector3(position[0], position[1], position[2])
	var direction := (Vector3(target[0], target[1], target[2]) - expected).normalized()
	var camera := app.get("_camera") as Camera3D
	if not _require(camera != null and camera.is_current(), "Player camera must remain current after GLB import"):
		return
	if not _require(camera.global_position.is_equal_approx(expected), "Camera position differs from manifest"):
		return
	if not _require((-camera.global_basis.z).is_equal_approx(direction) and is_equal_approx(camera.fov, float(manifest.camera.fov)), "Camera direction or FOV differs from manifest"):
		return
	if not _require(Input.mouse_mode == Input.MOUSE_MODE_VISIBLE, "Startup must not capture the mouse"):
		return
	var rain := app.get("_rain") as CPUParticles3D
	if not _require(rain != null and rain.amount > 0 and rain.emitting, "Native rain must be configured"):
		return
	app.call("_toggle_rain")
	if not _require(not rain.emitting, "Rain toggle must stop emission"):
		return
	app.call("_toggle_rain")
	app.call("_enter_scene")
	var key := InputEventKey.new()
	key.keycode = KEY_W
	key.pressed = true
	Input.parse_input_event(key)
	await process_frame
	app.call("_process", 0.5)
	key.pressed = false
	Input.parse_input_event(key)
	if not _require(not camera.global_position.is_equal_approx(expected), "W must move the walk camera"):
		return
	app.call("_reset_reference_camera")
	if not _require(camera.global_position.is_equal_approx(expected) and camera.is_current(), "Reset must restore the manifest camera"):
		return
	app.call("_release_mouse")
	print("GODOT_VERIFIED: GLB geometry, manifest camera, passive startup, native rain, walking and reset")
	quit(0)
