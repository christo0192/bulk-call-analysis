use std::path::{Path, PathBuf};
use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum OpenError {
    #[error("file not found")]
    FileNotFound,
    #[error("path is outside the project")]
    OutsideProject,
    #[error("file type is not allowed")]
    TypeNotAllowed,
    InvalidPath,
}

pub fn is_unsafe_path(path: &Path) -> bool {
    let text = path.to_string_lossy();
    text.starts_with(r"\\") || text.starts_with(r"\\?\UNC\") || text.starts_with(r"\\.\")
}

pub fn validate_project_path(
    project_dir: &Path,
    candidate: &Path,
    allowed_exts: &[&str],
) -> Result<PathBuf, OpenError> {
    if is_unsafe_path(project_dir) || is_unsafe_path(candidate) {
        return Err(OpenError::InvalidPath);
    }
    let project = dunce::canonicalize(project_dir).map_err(|_| OpenError::FileNotFound)?;
    let path = dunce::canonicalize(candidate).map_err(|_| OpenError::FileNotFound)?;
    if !path.starts_with(&project) {
        return Err(OpenError::OutsideProject);
    }
    if !path.is_file() {
        return Err(OpenError::FileNotFound);
    }
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("");
    if !allowed_exts
        .iter()
        .any(|allowed| extension.eq_ignore_ascii_case(allowed))
    {
        return Err(OpenError::TypeNotAllowed);
    }
    Ok(path)
}

pub fn validate_project_folder(project_dir: &Path) -> Result<PathBuf, OpenError> {
    if is_unsafe_path(project_dir) {
        return Err(OpenError::InvalidPath);
    }
    let path = dunce::canonicalize(project_dir).map_err(|_| OpenError::FileNotFound)?;
    if path.is_dir() {
        Ok(path)
    } else {
        Err(OpenError::FileNotFound)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn validates_inside_file_and_rejects_escape_extension_missing_and_directory() {
        let root = tempdir().unwrap();
        let project = root.path().join("project");
        fs::create_dir_all(&project).unwrap();
        let file = project.join("note.md");
        fs::write(&file, "x").unwrap();
        assert_eq!(
            validate_project_path(&project, &file, &["md"]).unwrap(),
            dunce::canonicalize(&file).unwrap()
        );
        let outside = root.path().join("outside.md");
        fs::write(&outside, "x").unwrap();
        assert_eq!(
            validate_project_path(&project, &project.join("..").join("outside.md"), &["md"]),
            Err(OpenError::OutsideProject)
        );
        assert_eq!(
            validate_project_path(&project, &outside, &["md"]),
            Err(OpenError::OutsideProject)
        );
        let bad = project.join("bad.exe");
        fs::write(&bad, "x").unwrap();
        assert_eq!(
            validate_project_path(&project, &bad, &["md"]),
            Err(OpenError::TypeNotAllowed)
        );
        assert_eq!(
            validate_project_path(&project, &project.join("missing.md"), &["md"]),
            Err(OpenError::FileNotFound)
        );
        let dir = project.join("folder.md");
        fs::create_dir(&dir).unwrap();
        assert_eq!(
            validate_project_path(&project, &dir, &["md"]),
            Err(OpenError::FileNotFound)
        );
    }

    #[test]
    fn validates_project_folder() {
        let root = tempdir().unwrap();
        assert!(validate_project_folder(root.path()).is_ok());
        assert_eq!(
            validate_project_folder(&root.path().join("missing")),
            Err(OpenError::FileNotFound)
        );
    }

    #[test]
    fn extension_matching_is_case_insensitive() {
        let root = tempdir().unwrap();
        let file = root.path().join("data.JSON");
        fs::write(&file, "{}").unwrap();
        assert!(validate_project_path(root.path(), &file, &["json"]).is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlink_escape_after_canonicalization() {
        use std::os::unix::fs::symlink;
        let root = tempdir().unwrap();
        let project = root.path().join("project");
        fs::create_dir(&project).unwrap();
        let outside = root.path().join("outside");
        fs::create_dir(&outside).unwrap();
        let file = outside.join("secret.txt");
        fs::write(&file, "x").unwrap();
        symlink(&outside, project.join("link")).unwrap();
        assert_eq!(
            validate_project_path(&project, &project.join("link/secret.txt"), &["txt"]),
            Err(OpenError::OutsideProject)
        );
    }

    #[cfg(windows)]
    #[test]
    fn rejects_junction_escape_after_canonicalization() {
        use std::os::windows::fs::symlink_dir;
        use std::process::Command;
        let root = tempdir().unwrap();
        let project = root.path().join("project");
        let outside = root.path().join("outside");
        fs::create_dir(&project).unwrap();
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("secret.txt"), "x").unwrap();
        let junction = project.join("link");
        if symlink_dir(&outside, &junction).is_err() {
            let status = Command::new("cmd")
                .args([
                    "/C",
                    "mklink",
                    "/J",
                    junction.to_str().unwrap(),
                    outside.to_str().unwrap(),
                ])
                .status()
                .unwrap();
            assert!(status.success());
        }
        assert_eq!(
            validate_project_path(&project, &junction.join("secret.txt"), &["txt"]),
            Err(OpenError::OutsideProject)
        );
    }
}
