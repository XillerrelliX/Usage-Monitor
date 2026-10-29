#![allow(dead_code, unused_imports)]
mod codex_environment;
#[path = "../upstream/codex_projects.rs"] mod codex_projects;
#[path = "../upstream/date.rs"] mod date;
mod db { include!(concat!(env!("OUT_DIR"), "/db_offline.rs")); }
#[path = "../upstream/overview.rs"] mod overview;
mod pricing { include!(concat!(env!("OUT_DIR"), "/pricing.rs")); }
mod project_sessions { include!(concat!(env!("OUT_DIR"), "/project_sessions_offline.rs")); }
mod scanner { include!(concat!(env!("OUT_DIR"), "/scanner_query.rs")); }
mod query_snapshot;
#[path = "../upstream/session_file.rs"] mod session_file;
#[path = "../upstream/session_index.rs"] mod session_index;
mod session_replay { include!(concat!(env!("OUT_DIR"), "/session_replay.rs")); }
#[path = "../upstream/types.rs"] mod types;

use rusqlite::{Connection, OpenFlags};
use std::{ffi::OsString, fs, path::{Path, PathBuf}, time::Duration};

const SOURCE_COMMIT: &str = "6bd0eed7b108497aa50051580387345340e3167f";
const APPLICATION_ID: i64 = 0x43554f46; // CUOF: Codex Usage Offline
type Result<T> = std::result::Result<T, &'static str>;

struct Args { home: PathBuf, database: PathBuf, timezone: String }

fn canonical(path: &Path) -> Result<PathBuf> {
    let path = fs::canonicalize(path).map_err(|_| "invalid_path")?;
    #[cfg(windows)] {
        let value = path.to_str().ok_or("invalid_path_encoding")?;
        if let Some(value) = value.strip_prefix("\\\\?\\UNC\\") { return Ok(PathBuf::from(format!("\\\\{value}"))); }
        if let Some(value) = value.strip_prefix("\\\\?\\") { return Ok(PathBuf::from(value)); }
    }
    Ok(path)
}

fn args() -> Result<Args> {
    let mut args = std::env::args_os().skip(1);
    let (mut home, mut database, mut timezone) = (None, None, None);
    while let Some(flag) = args.next() {
        let slot = match flag.to_str() {
            Some("--codex-home") => &mut home,
            Some("--database") => &mut database,
            Some("--timezone") => &mut timezone,
            _ => return Err("invalid_arguments"),
        };
        if slot.is_some() { return Err("duplicate_argument"); }
        *slot = Some(args.next().ok_or("missing_argument_value")?);
    }
    let timezone = match timezone {
        Some(value) => value.into_string().map_err(|_| "invalid_timezone")?,
        None => date::resolve_app_timezone(),
    };
    timezone.parse::<chrono_tz::Tz>().map_err(|_| "invalid_timezone")?;
    let home = canonical(&PathBuf::from(home.ok_or("missing_codex_home")?))
        .map_err(|_| "invalid_codex_home")?;
    if !home.is_dir() { return Err("invalid_codex_home"); }
    // Fail closed on missing roots or traversal errors; upstream's WalkDir
    // intentionally ignores traversal errors, which is unsuitable for replacement
    // of the only completed cache. This preflight never reads rollout contents.
    let mut roots = 0;
    for name in ["sessions", "archived_sessions"] {
        let path = home.join(name);
        if !path.exists() { continue; }
        if !path.is_dir() { return Err("invalid_sessions_directory"); }
        roots += 1;
        for entry in walkdir::WalkDir::new(path) {
            let entry = entry.map_err(|_| "unreadable_sessions_directory")?;
            if entry.file_type().is_file() && session_file::is_session_file(entry.path()) {
                fs::File::open(entry.path()).map_err(|_| "unreadable_session_file")?;
            }
        }
    }
    if roots == 0 { return Err("missing_sessions_directory"); }
    let requested = PathBuf::from(database.ok_or("missing_database")?);
    let filename = requested.file_name().ok_or("invalid_database_path")?;
    let parent = requested.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
    let parent = canonical(parent).map_err(|_| "invalid_database_parent")?;
    let database = if requested.exists() {
        canonical(&requested).map_err(|_| "invalid_database_path")?
    } else { parent.join(filename) };
    if database.starts_with(&home) || database.is_dir() { return Err("unsafe_database_path"); }
    Ok(Args { home, database, timezone })
}

fn meta(db: &Connection, key: &str) -> Result<String> {
    let sql = match key {
        "schema_version" => "SELECT CAST(schema_version AS TEXT) FROM offline_engine_meta",
        "source_commit" => "SELECT source_commit FROM offline_engine_meta",
        "codex_home" => "SELECT codex_home FROM offline_engine_meta",
        _ => return Err("invalid_database_marker"),
    };
    let count: i64 = db.query_row("SELECT COUNT(*) FROM offline_engine_meta", [], |row| row.get(0)).map_err(|_| "invalid_database_marker")?;
    if count != 1 { return Err("invalid_database_marker"); }
    db.query_row(sql, [], |row| row.get(0))
        .map_err(|_| "invalid_database_marker")
}

fn validate_existing(args: &Args) -> Result<()> {
    let db = Connection::open_with_flags(&args.database, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|_| "invalid_database")?;
    let id: i64 = db.pragma_query_value(None, "application_id", |row| row.get(0))
        .map_err(|_| "invalid_database")?;
    if id != APPLICATION_ID || meta(&db, "schema_version")? != "1" || meta(&db, "source_commit")? != SOURCE_COMMIT {
        return Err("foreign_database");
    }
    if meta(&db, "codex_home")? != args.home.to_string_lossy() { return Err("database_source_mismatch"); }
    let check: String = db.query_row("PRAGMA quick_check", [], |row| row.get(0)).map_err(|_| "invalid_database")?;
    if check != "ok" { return Err("invalid_database"); }
    // Validate required column names/types before any upstream open/migration.
    for (table, required) in [
        ("daily_usage_rollups", &["date", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens", "cost_usd", "models_json", "projects_json", "updated_at"][..]),
        ("session_file_rollups", &["path", "modified_at_ms", "size_bytes", "rows_json", "prompt_title", "turn_count", "quota_usage_json", "quota_parser_version", "updated_at"][..]),
        ("scan_runs", &["id", "scanned_at", "timezone", "imported_days"][..]),
        ("project_session_index", &["path", "source_updated_at", "revision", "detail_json", "search_text"][..]),
        ("project_session_days", &["project", "date", "path", "total_tokens", "cost_usd", "usage_json"][..]),
        ("project_session_quota", &["path", "date", "kind", "end_at", "reset_at", "end_percent"][..]),
    ] {
        let mut statement = db.prepare(&format!("PRAGMA table_info({table})")).map_err(|_| "invalid_database_schema")?;
        let columns: Vec<String> = statement.query_map([], |row| row.get(1)).map_err(|_| "invalid_database_schema")?
            .collect::<std::result::Result<_, _>>().map_err(|_| "invalid_database_schema")?;
        if columns.len() != required.len() || !required.iter().all(|name| columns.iter().any(|column| column == name)) {
            return Err("invalid_database_schema");
        }
    }
    let timezone: Option<String> = db.query_row("SELECT timezone FROM scan_runs ORDER BY id DESC LIMIT 1", [], |row| row.get(0))
        .map(Some).or_else(|e| match e { rusqlite::Error::QueryReturnedNoRows => Ok(None), _ => Err(e) })
        .map_err(|_| "invalid_database_schema")?;
    if timezone.as_deref().is_some_and(|old| old != args.timezone) { return Err("database_timezone_mismatch"); }
    Ok(())
}

fn run() -> Result<serde_json::Value> {
    if std::env::args_os().skip(1).any(|arg| arg == "--query-snapshot") {
        return query_snapshot::run();
    }
    let args = args()?;
    if args.database.exists() { validate_existing(&args)?; }
    else {
        // create_new prevents accidentally adopting a concurrently created DB.
        fs::OpenOptions::new().write(true).create_new(true).open(&args.database).map_err(|_| "database_create_failed")?;
        let mut db = Connection::open(&args.database).map_err(|_| "database_create_failed")?;
        let tx = db.transaction().map_err(|_| "database_create_failed")?;
        tx.pragma_update(None, "application_id", APPLICATION_ID).map_err(|_| "database_create_failed")?;
        tx.execute_batch("CREATE TABLE offline_engine_meta (schema_version INTEGER NOT NULL, source_commit TEXT NOT NULL, codex_home TEXT NOT NULL);")
            .map_err(|_| "database_create_failed")?;
        tx.execute("INSERT INTO offline_engine_meta (schema_version,source_commit,codex_home) VALUES (1,?,?)",
            [SOURCE_COMMIT, args.home.to_str().ok_or("invalid_codex_home_encoding")?]).map_err(|_| "database_create_failed")?;
        tx.commit().map_err(|_| "database_create_failed")?;
    }
    codex_environment::configure(args.home.clone()).map_err(|_| "source_configuration_failed")?;
    let mut db = db::open_database(&args.database).map_err(|_| "database_open_failed")?;
    db.busy_timeout(Duration::from_secs(30)).map_err(|_| "database_open_failed")?;
    let before: i64 = db.query_row("SELECT COALESCE(MAX(id), 0) FROM scan_runs", [], |row| row.get(0)).map_err(|_| "invalid_database_schema")?;
    // No external cache path: this harness uses upstream embedded prices only.
    let pricing = pricing::PricingSource::load_cached_or_embedded(None);
    let scan = scanner::scan_codex_usage(&mut db, &pricing, Some(args.home), Some(args.timezone)).map_err(|_| "scan_failed")?;
    let (id, scanned_at, timezone, days): (i64,String,String,i64) = db.query_row(
        "SELECT id,scanned_at,timezone,imported_days FROM scan_runs ORDER BY id DESC LIMIT 1", [],
        |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?))).map_err(|_| "scan_completion_missing")?;
    if id <= before || scanned_at != scan.scanned_at || timezone != scan.timezone || days != scan.imported_days as i64 {
        return Err("scan_completion_mismatch");
    }
    let checkpoint: (i64,i64,i64) = db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [],
        |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?))).map_err(|_| "database_checkpoint_failed")?;
    if checkpoint.0 != 0 { return Err("database_checkpoint_busy"); }
    db.close().map_err(|_| "database_close_failed")?;
    Ok(serde_json::json!({
        "schemaVersion":1, "engineVersion":env!("CARGO_PKG_VERSION"), "sourceCommit":SOURCE_COMMIT,
        "scanId":id, "scannedAt":scanned_at, "timezone":timezone, "importedDays":days,
        "filesScanned":scan.metrics.files_scanned, "filesParsed":scan.metrics.files_parsed,
        "filesReused":scan.metrics.files_reused, "bytesRead":scan.metrics.bytes_read
    }))
}

fn main() {
    match run() {
        Ok(result) => println!("{result}"),
        Err(code) => {
            // Static codes only: never echo user data or parser error strings.
            eprintln!("{}", serde_json::json!({"schemaVersion":1,"errorCode":code}));
            std::process::exit(1);
        }
    }
}
