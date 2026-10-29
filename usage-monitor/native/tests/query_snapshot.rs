use serde_json::{json, Value};
use std::{fs, path::PathBuf, process::{Command, Output}};

const ID: &str = "01977e3d-d9f6-72b7-93cf-f3f2f83c382c";
const PARENT: &str = "01977e3d-d9f6-72b7-93cf-f3f2f83c382d";
const PRIVATE: &str = "PRIVATE_MESSAGE_ARGUMENT_OUTPUT_TITLE_99712";

struct Fixture { root: PathBuf, path: PathBuf }
impl Fixture {
    fn new(records: Vec<Value>) -> Self {
        let root = std::env::temp_dir().join(format!("query-snapshot-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join(format!("rollout-2026-10-28T09-00-00-{ID}.jsonl"));
        fs::write(&path, records.iter().map(|value| format!("{value}\n")).collect::<String>()).unwrap();
        Self { root, path }
    }
    fn run(&self, turn: Option<&str>, limit: Option<usize>) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_codex-usage-offline"));
        command.arg("--query-snapshot").arg(&self.path);
        if let Some(turn) = turn { command.arg("--turn-id").arg(turn); }
        if let Some(limit) = limit { command.arg("--byte-limit").arg(limit.to_string()); }
        command.output().unwrap()
    }
    fn value(&self, turn: Option<&str>, limit: Option<usize>) -> Value {
        let result = self.run(turn, limit);
        assert!(result.status.success(), "{}", String::from_utf8_lossy(&result.stderr));
        assert!(result.stderr.is_empty());
        assert!(!String::from_utf8_lossy(&result.stdout).contains(PRIVATE));
        serde_json::from_slice(&result.stdout).unwrap()
    }
    fn error(&self, expected: &str) {
        let result = self.run(None, None);
        assert!(!result.status.success()); assert!(result.stdout.is_empty());
        let error: Value = serde_json::from_slice(&result.stderr).unwrap();
        assert_eq!(error["errorCode"], expected);
        assert!(!String::from_utf8_lossy(&result.stderr).contains(PRIVATE));
    }
}
impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.root); } }

fn record(kind: &str, payload: Value) -> Value {
    json!({"timestamp":"2026-10-28T09:00:00Z","type":kind,"payload":payload})
}
fn meta() -> Value { record("session_meta", json!({"id":ID,"cwd":PRIVATE,"title":PRIVATE})) }
fn start(id: &str) -> Value { record("turn_context", json!({"turn_id":id,"model":"gpt-5","cwd":PRIVATE})) }
fn event(kind: &str, id: &str) -> Value { record("event_msg", json!({"type":kind,"turn_id":id})) }
fn usage(input: i64, cached: i64, output: i64, reasoning: i64, total: i64) -> Value {
    json!({"input_tokens":input,"cached_input_tokens":cached,"output_tokens":output,
           "reasoning_output_tokens":reasoning,"total_tokens":total})
}
fn token(last: Option<Value>, total: Option<Value>) -> Value {
    let mut info = json!({});
    if let Some(last) = last { info["last_token_usage"] = last; }
    if let Some(total) = total { info["total_token_usage"] = total; }
    record("event_msg", json!({"type":"token_count","info":info}))
}
fn call(name: &str, id: &str) -> Value {
    record("response_item", json!({"type":"function_call","name":name,"call_id":id,"arguments":PRIVATE}))
}
fn values(input: i64, cached: i64, output: i64, reasoning: i64, total: i64) -> Value {
    json!({"inputTokens":input,"cachedInputTokens":cached,"outputTokens":output,
           "reasoningOutputTokens":reasoning,"totalTokens":total})
}

#[test]
fn cumulative_checkpoints_reconstruct_turn_start_and_exclude_later_turns() {
    let fixture = Fixture::new(vec![meta(), start("old"), token(Some(usage(100,20,40,7,140)),None),
        event("task_complete","old"), start("selected"), event("task_started","selected"),
        token(Some(usage(70,60,20,4,90)),None), event("task_completed","selected"),
        start("later"), token(Some(usage(999,0,1,0,1000)),None)]);
    let result = fixture.value(Some("selected"), None);
    assert_eq!(result["beforeTurn"], values(100,20,40,7,140));
    assert_eq!(result["throughTurn"], values(170,80,60,11,230));
    assert_eq!(result["turnStatus"],"completed");
    assert_eq!(result["turnId"],"selected");
    assert_eq!(fixture.value(None,None)["turnId"],"later");
    assert_eq!(result["sessionId"], ID);
    assert_eq!(result["agentPath"],"/root");
    assert!(result["parentSessionId"].is_null());
    assert_eq!(result["completeLines"],10);
    assert_eq!(result["trailingPartial"],false);
    assert_eq!(fs::read_dir(&fixture.root).unwrap().count(),1);
}

#[test]
fn canonical_total_deltas_resets_last_precedence_and_subsets_are_preserved() {
    let fixture = Fixture::new(vec![meta(), start("one"),
        token(None,Some(usage(100,30,20,4,120))),
        token(None,Some(usage(180,70,60,8,240))),
        token(None,Some(usage(10,2,5,1,15))),
        token(Some(usage(3,9,2,7,8)),Some(usage(100,30,20,4,120))),
        token(Some(usage(0,0,0,0,99)),None)]);
    // Canonical reset clamps negative differences; cached stays a subset, while
    // reasoning and recorded total remain upstream's original values.
    assert_eq!(fixture.value(None,None)["throughTurn"],values(183,73,62,15,248));
}

#[test]
fn fixed_byte_prefix_is_exact_and_a_partial_tail_is_never_counted() {
    let fixture = Fixture::new(vec![meta(),start("one"), token(Some(usage(10,3,2,1,12)),None)]);
    let boundary=fs::metadata(&fixture.path).unwrap().len() as usize;
    use std::io::Write;
    let mut file=fs::OpenOptions::new().append(true).open(&fixture.path).unwrap();
    write!(file,"{}",event("task_complete","one")).unwrap();
    let initial=fixture.value(None,Some(boundary));
    assert_eq!(initial["turnStatus"],"active");
    assert_eq!(initial["trailingPartial"],false);
    let partial=fixture.value(None,None);
    assert_eq!(partial["trailingPartial"],true);
    assert_eq!(partial["turnStatus"],"active");
    assert_eq!(partial["throughTurn"],initial["throughTurn"]);
    writeln!(file).unwrap();
    assert_eq!(fixture.value(None,None)["turnStatus"],"completed");
    assert_eq!(fixture.value(None,Some(boundary))["readBytes"],boundary);
}

#[test]
fn structured_dispatches_require_call_ids_and_never_parse_arguments_or_outputs() {
    let fixture=Fixture::new(vec![meta(),start("one"),call("functions.spawn_agent","call-1"),
        call("collaboration.followup_task","call-2"),call("collaboration.send_message","call-3"),
        call("send_input","call-4"),call("exec_command","call-5"),
        record("response_item",json!({"type":"function_call_output","call_id":"call-5","output":"spawn_agent"})),
        call("functions.spawn_agent","call-1")]);
    let result=fixture.value(None,None);
    assert_eq!(result["delegationCalls"],json!([
        {"id":"call-1","kind":"spawn_agent"},{"id":"call-2","kind":"followup_task"},
        {"id":"call-3","kind":"send_message_to_agent"},{"id":"call-4","kind":"send_input"}]));
    assert_eq!(result.as_object().unwrap().len(),14);
    let missing=Fixture::new(vec![meta(),start("one"),record("response_item",json!({"type":"function_call","name":"spawn_agent","arguments":PRIVATE}))]);
    missing.error("query_invalid_dispatch_id");
}

#[test]
fn lineage_is_only_explicit_own_session_metadata_never_fork_ancestry() {
    let mut metadata=meta();
    metadata["payload"]["forked_from_id"]=json!(PARENT);
    let fork=Fixture::new(vec![metadata.clone(),start("one")]);
    assert!(fork.value(None,None)["parentSessionId"].is_null());
    metadata["payload"]["source"]=json!({"subagent":{"thread_spawn":{"parent_thread_id":PARENT,"agent_path":"/root/worker"}}});
    let child=Fixture::new(vec![metadata.clone(),start("one")]);
    assert_eq!(child.value(None,None)["parentSessionId"],PARENT);
    assert_eq!(child.value(None,None)["agentPath"],"/root/worker");
    metadata["payload"]["id"]=json!(PARENT);
    Fixture::new(vec![metadata,start("one")]).error("query_session_id_mismatch");
}

#[test]
fn terminal_status_requires_a_matching_lifecycle_marker() {
    for (marker,status) in [("task_complete","completed"),("task_completed","completed"),
        ("turn_aborted","interrupted"),("task_failed","failed")] {
        let fixture=Fixture::new(vec![meta(),start("one"),token(Some(usage(0,0,0,0,0)),None),event(marker,"one")]);
        let result=fixture.value(None,None);
        assert_eq!(result["turnStatus"],status); assert!(result["completedAt"].is_string());
    }
    let fixture=Fixture::new(vec![meta(),start("one"),record("response_item",json!({"type":"message","role":"assistant","content":PRIVATE}))]);
    assert_eq!(fixture.value(None,None)["turnStatus"],"active");
    Fixture::new(vec![meta(),start("one"),event("task_complete","wrong")]).error("query_conflicting_turn_id");
}

#[test]
fn completion_without_recorded_usage_is_missing_and_explicit_zero_remains_zero() {
    let active=Fixture::new(vec![meta(),start("one")]);
    assert_eq!(active.value(None,None)["turnStatus"],"active");
    Fixture::new(vec![meta(),start("one"),event("task_complete","one")])
        .error("query_missing_token_usage");
    Fixture::new(vec![meta(),start("one"),token(None,None),event("task_complete","one")])
        .error("query_missing_token_usage");
    Fixture::new(vec![meta(),start("one"),record("event_msg",json!({"type":"token_count"})),event("task_complete","one")])
        .error("query_missing_token_usage");
    Fixture::new(vec![meta(),start("one"),record("event_msg",json!({"type":"token_count","info":null})),event("task_complete","one")])
        .error("query_missing_token_usage");
    Fixture::new(vec![meta(),start("one"),token(Some(json!({})),None),event("task_complete","one")])
        .error("query_missing_token_usage");
    let zero=Fixture::new(vec![meta(),start("one"),token(Some(usage(0,0,0,0,0)),None),event("task_complete","one")]);
    let result=zero.value(None,None);
    assert_eq!(result["turnStatus"],"completed");
    assert_eq!(result["beforeTurn"],values(0,0,0,0,0));
    assert_eq!(result["throughTurn"],values(0,0,0,0,0));
}

#[test]
fn missing_conflicting_and_noncontiguous_turns_fail_closed() {
    Fixture::new(vec![meta(),token(Some(usage(1,0,1,0,2)),None)]).error("query_missing_turn_id");
    Fixture::new(vec![meta(),record("turn_context",json!({"model":"gpt-5"}))]).error("query_missing_turn_id");
    Fixture::new(vec![meta(),start("one"),start("two"),start("one")]).error("query_noncontiguous_turn_id");
    let mut conflicting=start("one"); conflicting["turnId"]=json!("other");
    Fixture::new(vec![meta(),conflicting]).error("query_conflicting_turn_id");
    let fixture=Fixture::new(vec![meta(),start("one")]);
    let result=fixture.run(Some("absent"),None);
    assert_eq!(serde_json::from_slice::<Value>(&result.stderr).unwrap()["errorCode"],"query_turn_not_found");
}

#[test]
fn malformed_complete_records_and_incomplete_selected_records_fail_closed() {
    let fixture=Fixture::new(vec![meta(),start("one")]);
    use std::io::Write;
    writeln!(fs::OpenOptions::new().append(true).open(&fixture.path).unwrap(),"{PRIVATE}").unwrap();
    fixture.error("query_malformed_record");
    Fixture::new(vec![meta(),start("one"),json!({"type":"event_msg","payload":null})]).error("query_incomplete_record");
    let mut invalid=token(Some(usage(1,0,1,0,2)),None); invalid["timestamp"]=json!(PRIVATE);
    Fixture::new(vec![meta(),start("one"),invalid]).error("query_invalid_timestamp");
}

#[test]
fn negative_fractional_large_and_overflowing_usage_numbers_fail_closed() {
    for invalid in [json!(-1),json!(1.5),json!("12"),json!(null),json!(u64::MAX)] {
        let mut numbers=usage(1,0,1,0,2); numbers["input_tokens"]=invalid;
        Fixture::new(vec![meta(),start("one"),token(Some(numbers),None)]).error("query_invalid_usage_number");
    }
    Fixture::new(vec![meta(),start("one"),token(Some(usage(i64::MAX,0,1,0,0)),None)])
        .error("query_numeric_overflow");
    Fixture::new(vec![meta(),start("one"),token(Some(usage(9_007_199_254_740_992,0,0,0,1)),None)])
        .error("query_unsafe_integer");
}

#[test]
fn compressed_query_sources_are_explicitly_rejected_without_creating_a_database() {
    let fixture=Fixture::new(vec![meta(),start("one")]);
    let compressed=fixture.path.with_extension("jsonl.zst");
    fs::write(&compressed,zstd::stream::encode_all(fs::read(&fixture.path).unwrap().as_slice(),1).unwrap()).unwrap();
    let result=Command::new(env!("CARGO_BIN_EXE_codex-usage-offline"))
        .arg("--query-snapshot").arg(compressed).output().unwrap();
    assert!(!result.status.success()); assert!(result.stdout.is_empty());
    assert_eq!(serde_json::from_slice::<Value>(&result.stderr).unwrap()["errorCode"],"query_compressed_source_unsupported");
    assert_eq!(fs::read_dir(&fixture.root).unwrap().count(),2);
}

#[test]
fn byte_limits_duplicate_flags_and_mixed_modes_fail_closed() {
    let fixture=Fixture::new(vec![meta(),start("one")]);
    let result=fixture.run(None,Some(usize::MAX));
    assert!(!result.status.success());
    for flags in [vec!["--database","secret.sqlite"],vec!["--turn-id","one","--turn-id","one"]] {
        let result=Command::new(env!("CARGO_BIN_EXE_codex-usage-offline"))
            .arg("--query-snapshot").arg(&fixture.path).args(flags).output().unwrap();
        assert!(!result.status.success()); assert!(result.stdout.is_empty());
    }
}
