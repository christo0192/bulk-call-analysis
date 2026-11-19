fn main() {
    let attributes = tauri_build::Attributes::new()
        .windows_attributes(tauri_build::WindowsAttributes::new_without_app_manifest());
    tauri_build::try_build(attributes).expect("failed to run tauri-build");

    // tauri-build does not provide an app manifest because of
    // `new_without_app_manifest()`. `compile_for_everything` emits one
    // universal `rustc-link-arg`, which Cargo applies exactly once to the app,
    // integration-test, and lib-unit-test artifacts.
    if std::env::var("TARGET").is_ok_and(|target| target.contains("windows")) {
        println!("cargo:rerun-if-changed=windows-test-manifest.rc");
        println!("cargo:rerun-if-changed=windows-test-manifest.xml");
        embed_resource::compile_for_everything("windows-test-manifest.rc", embed_resource::NONE)
            .manifest_required()
            .expect("failed to embed the Windows test manifest");
    }
}
