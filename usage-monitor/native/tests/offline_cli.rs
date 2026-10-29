use rusqlite::Connection;
use serde_json::{json, Value};
use std::{fs, path::PathBuf, process::{Command, Output}};

const ID: &str = "01977e3d-d9f6-72b7-93cf-f3f2f83c382c";
const SENTINEL: &str = "PRIVATE_PROMPT_AND_OFFICIAL_TITLE_SENTINEL_781449";

struct Fixture { root: PathBuf, home: PathBuf, database: PathBuf }
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("offline-cli-{}",uuid::Uuid::new_v4()));
        let home = root.join("codex");
        fs::create_dir_all(home.join("sessions")).unwrap();
        let database = root.join("owned.sqlite");
        Self { root,home,database }
    }
    fn run(&self, timezone: &str) -> Output {
        Command::new(env!("CARGO_BIN_EXE_codex-usage-offline"))
            .arg("--codex-home").arg(&self.home).arg("--database").arg(&self.database)
            .arg("--timezone").arg(timezone).output().unwrap()
    }
    fn success(&self) -> Value {
        let output = self.run("UTC");
        assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));
        assert!(output.stderr.is_empty());
        let result: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(result["schemaVersion"],1);
        assert_eq!(result["sourceCommit"],"6bd0eed7b108497aa50051580387345340e3167f");
        result
    }
    fn path(&self) -> PathBuf { self.home.join("sessions").join(format!("rollout-2026-09-01T09-00-00-{ID}.jsonl")) }
    fn write(&self, input: i64) { fs::write(self.path(), log(input)).unwrap(); }
}
impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.root); } }

fn log(input: i64) -> String {
    [
        json!({"timestamp":"2026-09-01T09:00:00Z","type":"session_meta","payload":{"id":ID,"cwd":"/synthetic/project","source":{"subagent":{"thread_spawn":{"parent_thread_id":"synthetic-parent","depth":1,"agent_path":"/root/child","agent_nickname":"Curie","agent_role":"worker"}}}}}),
        json!({"timestamp":"2026-09-01T09:00:00Z","type":"turn_context","payload":{"model":"gpt-5","cwd":"/synthetic/project","turn_id":"turn-1"}}),
        json!({"timestamp":"2026-09-01T09:00:00Z","type":"event_msg","payload":{"type":"user_message","message":SENTINEL,"turn_id":"turn-1"}}),
        json!({"timestamp":"2026-09-01T09:00:00Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":SENTINEL}]}}),
        json!({"timestamp":"2026-09-01T09:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":input,"cached_input_tokens":20,"output_tokens":40,"reasoning_output_tokens":7,"total_tokens":input+40}}}}),
    ].iter().map(Value::to_string).collect::<Vec<_>>().join("\n")
}

#[test]
fn production_preserves_all_five_counters_and_lineage_without_persisting_contents() {
    let fixture = Fixture::new();
    fixture.write(100);
    fs::write(fixture.home.join("session_index.jsonl"),json!({"id":ID,"thread_name":SENTINEL}).to_string()).unwrap();
    // Authentication files are deliberately unreadable data; no code path needs them.
    fs::write(fixture.home.join("auth.json"),"not an authentication fixture").unwrap();
    let result = fixture.success();
    assert_eq!(result["filesScanned"],1);
    assert_eq!(result["filesParsed"],1);
    assert_eq!(result["importedDays"],1);
    let db = Connection::open(&fixture.database).unwrap();
    let counters:(i64,i64,i64,i64,i64) = db.query_row("SELECT input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens FROM daily_usage_rollups",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).unwrap();
    assert_eq!(counters,(100,20,40,7,140));
    let rows:String = db.query_row("SELECT rows_json FROM session_file_rollups",[],|r|r.get(0)).unwrap();
    let rows:Value = serde_json::from_str(&rows).unwrap();
    assert_eq!(rows[0]["models"]["gpt-5"]["reasoningOutputTokens"],7);
    assert_eq!(rows[0]["projects"]["/synthetic/project"]["totalTokens"],140);
    let count:i64=db.query_row("SELECT COUNT(*) FROM session_file_rollups WHERE prompt_title IS NOT NULL",[],|r|r.get(0)).unwrap();
    assert_eq!(count,0);
    let (detail,search):(String,String)=db.query_row("SELECT detail_json,search_text FROM project_session_index",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();
    assert_eq!(search,"");
    let detail:Value=serde_json::from_str(&detail).unwrap();
    assert_eq!(detail.as_object().unwrap().len(),5);
    assert_eq!(detail["agentSessionId"],ID);
    assert_eq!(detail["parentSessionId"],"synthetic-parent");
    assert_eq!(detail["agentPath"],"/root/child");
    assert_eq!(detail["agentNickname"],"Curie");
    assert_eq!(detail["agentRole"],"worker");
    drop(db);
    for file in fs::read_dir(&fixture.root).unwrap().filter_map(Result::ok).filter(|e|e.file_type().unwrap().is_file()) {
        let bytes=fs::read(file.path()).unwrap();
        assert!(!bytes.windows(SENTINEL.len()).any(|part|part==SENTINEL.as_bytes()));
    }
    let second=fixture.success();
    assert_eq!(second["scanId"],2);
    assert_eq!(second["filesParsed"],0);
    assert_eq!(second["filesReused"],1);
    let db=Connection::open(&fixture.database).unwrap();
    let total:i64=db.query_row("SELECT SUM(total_tokens) FROM daily_usage_rollups",[],|r|r.get(0)).unwrap();
    assert_eq!(total,140);
}

#[test]
fn production_scan_failure_rolls_back_completed_cache_and_reports_only_static_code() {
    let fixture=Fixture::new(); fixture.write(100); fixture.success();
    let db=Connection::open(&fixture.database).unwrap();
    db.execute_batch("CREATE TRIGGER reject_scan BEFORE INSERT ON scan_runs BEGIN SELECT RAISE(ABORT, 'PRIVATE_PROMPT_AND_OFFICIAL_TITLE_SENTINEL_781449'); END;").unwrap();
    drop(db);
    fixture.write(999);
    let output=fixture.run("UTC");
    assert!(!output.status.success()); assert!(output.stdout.is_empty());
    assert_eq!(serde_json::from_slice::<Value>(&output.stderr).unwrap()["errorCode"],"scan_failed");
    let db=Connection::open(&fixture.database).unwrap();
    let (tokens,scans):(i64,i64)=db.query_row("SELECT (SELECT SUM(total_tokens) FROM daily_usage_rollups),(SELECT COUNT(*) FROM scan_runs)",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();
    assert_eq!((tokens,scans),(140,1));
}

#[test]
fn production_refuses_stock_database_source_timezone_and_schema_mismatches() {
    let fixture=Fixture::new(); fixture.write(100);
    let db=Connection::open(&fixture.database).unwrap();db.execute_batch("CREATE TABLE desktop_data(secret TEXT)").unwrap();drop(db);
    let before=fs::read(&fixture.database).unwrap();
    assert!(!fixture.run("UTC").status.success());
    assert_eq!(before,fs::read(&fixture.database).unwrap());
    fs::remove_file(&fixture.database).unwrap();fixture.success();
    assert!(!fixture.run("Europe/Berlin").status.success());
    let db=Connection::open(&fixture.database).unwrap();
    db.execute("UPDATE offline_engine_meta SET codex_home='different-source'",[]).unwrap();drop(db);
    assert!(!fixture.run("UTC").status.success());
    let db=Connection::open(&fixture.database).unwrap();
    db.execute("UPDATE offline_engine_meta SET codex_home=?",[fixture.home.to_string_lossy().to_string()]).unwrap();
    db.execute_batch("ALTER TABLE session_file_rollups RENAME TO wrong_schema").unwrap();drop(db);
    assert!(!fixture.run("UTC").status.success());
}

#[test]
fn production_compressed_archive_and_active_copy_count_only_once() {
    let fixture=Fixture::new(); fixture.write(100); fixture.success();
    let archive=fixture.home.join("archived_sessions");fs::create_dir(&archive).unwrap();
    let compressed=archive.join(format!("rollout-2026-09-01T09-00-00-{ID}.jsonl.zst"));
    fs::write(&compressed,zstd::stream::encode_all(log(100).as_bytes(),1).unwrap()).unwrap();
    let result=fixture.success();assert_eq!(result["filesScanned"],1);
    fs::remove_file(fixture.path()).unwrap();
    let result=fixture.success();assert_eq!(result["filesScanned"],1);
    let db=Connection::open(&fixture.database).unwrap();
    let (tokens,files):(i64,i64)=db.query_row("SELECT (SELECT SUM(total_tokens) FROM daily_usage_rollups),(SELECT COUNT(*) FROM session_file_rollups)",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();
    assert_eq!((tokens,files),(140,1));
}

#[test]
fn production_validates_paths_arguments_and_empty_success() {
    let fixture=Fixture::new();
    let result=fixture.success(); assert_eq!(result["importedDays"],0);assert_eq!(result["filesScanned"],0);
    assert!(!fixture.run("not-a-timezone").status.success());
    let output=Command::new(env!("CARGO_BIN_EXE_codex-usage-offline"))
        .arg("--codex-home").arg(&fixture.home).arg("--database").arg(fixture.home.join("unsafe.sqlite")).output().unwrap();
    assert!(!output.status.success());assert!(!fixture.home.join("unsafe.sqlite").exists());
    let output=Command::new(env!("CARGO_BIN_EXE_codex-usage-offline")).output().unwrap();
    assert!(!output.status.success());assert!(output.stdout.is_empty());
}
