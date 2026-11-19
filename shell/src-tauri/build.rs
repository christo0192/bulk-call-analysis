fn main() {
    let attributes = tauri_build::Attributes::new()
        .windows_attributes(tauri_build::WindowsAttributes::new_without_app_manifest());
    tauri_build::try_build(attributes).expect("failed to run tauri-build");

    // tauri-build links its resource only to binary targets. Lib unit tests are
    // separate Windows executables, so give them the same manifest explicitly.
    if std::env::var("TARGET").is_ok_and(|target| target.contains("windows")) {
        println!("cargo:rerun-if-changed=windows-test-manifest.rc");
        println!("cargo:rerun-if-changed=windows-test-manifest.xml");
        embed_resource::compile_for_tests("windows-test-manifest.rc", embed_resource::NONE)
            .manifest_required()
            .expect("failed to embed the Windows test manifest");
        let target_env = std::env::var("CARGO_CFG_TARGET_ENV").expect("CARGO_CFG_TARGET_ENV");
        let resource_name = if target_env == "msvc" {
            "windows-test-manifest.lib"
        } else {
            "libwindows-test-manifest.a"
        };
        let resource = std::path::Path::new(&std::env::var_os("OUT_DIR").expect("OUT_DIR"))
            .join(resource_name);
        println!("cargo:rustc-link-arg={}", resource.display());
    }
}
