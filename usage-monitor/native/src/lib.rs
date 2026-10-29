//! Original upstream test harness. Production persistence patches are exercised
//! separately by CLI integration tests; these modules retain upstream assertions.
#![allow(dead_code, unused_imports)]
mod codex_environment;
#[path = "../upstream/codex_projects.rs"] mod codex_projects;
#[path = "../upstream/date.rs"] mod date;
#[path = "../upstream/db.rs"] mod db;
#[path = "../upstream/overview.rs"] mod overview;
mod pricing { include!(concat!(env!("OUT_DIR"), "/pricing.rs")); }
#[path = "../upstream/project_sessions.rs"] mod project_sessions;
#[path = "../upstream/scanner.rs"] mod scanner;
#[path = "../upstream/session_file.rs"] mod session_file;
#[path = "../upstream/session_index.rs"] mod session_index;
mod session_replay { include!(concat!(env!("OUT_DIR"), "/session_replay.rs")); }
#[path = "../upstream/types.rs"] mod types;
