//! Explicit-source shim. It performs no source discovery, authentication read,
//! subprocess launch, WSL lookup, remote access, or implicit home fallback.
use std::{path::PathBuf, sync::OnceLock};

pub(crate) struct CodexEnvironment {
    pub home: PathBuf,
}

static SELECTED: OnceLock<CodexEnvironment> = OnceLock::new();

pub(crate) fn configure(home: PathBuf) -> Result<(), String> {
    SELECTED.set(CodexEnvironment { home }).map_err(|_| "Source already configured".to_string())
}

pub(crate) fn selected_codex_environment() -> &'static CodexEnvironment {
    SELECTED.get().expect("An explicit --codex-home is required")
}
