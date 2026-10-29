//! Read one fixed rollout prefix, exposing numeric and explicit scope evidence only.
use crate::scanner::QueryUsageCounter;
use serde_json::{json, Value};
use std::{collections::{BTreeMap, BTreeSet}, fs::File, io::Read, path::{Path, PathBuf}};

type Result<T> = std::result::Result<T, &'static str>;
const MAX_SAFE_INTEGER: i64 = 9_007_199_254_740_991;

struct Turn {
    before: [i64; 5], through: [i64; 5], status: &'static str,
    started: String, completed: Option<String>, calls: BTreeMap<String, String>,
    usage_recorded: bool,
}

fn safe_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 256 && value.bytes().all(|byte|
        byte.is_ascii_alphanumeric() || b"-_./:".contains(&byte))
}

fn uuid(value: &str) -> bool {
    value.len() == 36 && value.bytes().enumerate().all(|(index, byte)|
        if [8, 13, 18, 23].contains(&index) { byte == b'-' } else { byte.is_ascii_hexdigit() })
}

fn timestamp(entry: &Value) -> Result<String> {
    let value = entry.get("timestamp").and_then(Value::as_str).ok_or("query_missing_timestamp")?;
    chrono::DateTime::parse_from_rfc3339(value).map_err(|_| "query_invalid_timestamp")?;
    Ok(value.to_string())
}

fn explicit_id(entry: &Value, payload: &Value) -> Result<Option<String>> {
    let mut found: Option<&str> = None;
    for object in [entry, payload] {
        for key in ["turn_id", "turnId"] {
            if let Some(value) = object.get(key) {
                let value = value.as_str().filter(|value| safe_id(value)).ok_or("query_invalid_turn_id")?;
                if found.is_some_and(|previous| previous != value) { return Err("query_conflicting_turn_id"); }
                found = Some(value);
            }
        }
    }
    Ok(found.map(ToString::to_string))
}

fn delegation_kind(name: &str) -> Option<&'static str> {
    // Match structured tool names, never arguments, messages or result bodies.
    let bare = name.strip_prefix("functions.").unwrap_or(name);
    let bare = bare.strip_prefix("collaboration.").unwrap_or(bare);
    match bare {
        "spawn_agent" => Some("spawn_agent"),
        "followup_task" => Some("followup_task"),
        "send_message_to_agent" => Some("send_message_to_agent"),
        "send_input" => Some("send_input"),
        "send_message" if name.contains("collaboration.") => Some("send_message_to_agent"),
        _ => None,
    }
}

fn counters(values: [i64; 5]) -> Value {
    json!({"inputTokens":values[0],"cachedInputTokens":values[1],"outputTokens":values[2],
           "reasoningOutputTokens":values[3],"totalTokens":values[4]})
}

pub(crate) fn run() -> Result<Value> {
    let mut args = std::env::args_os().skip(1);
    let (mut path, mut turn, mut limit) = (None, None, None);
    while let Some(flag) = args.next() {
        let slot = match flag.to_str() {
            Some("--query-snapshot") => &mut path,
            Some("--turn-id") => &mut turn,
            Some("--byte-limit") => &mut limit,
            _ => return Err("invalid_arguments"),
        };
        if slot.is_some() { return Err("duplicate_argument"); }
        *slot = Some(args.next().ok_or("missing_argument_value")?);
    }
    let path = PathBuf::from(path.ok_or("query_missing_source")?);
    if path.extension().is_some_and(|extension| extension == "zst") {
        return Err("query_compressed_source_unsupported");
    }
    let path = crate::canonical(&path).map_err(|_| "query_invalid_source")?;
    if path.extension().is_none_or(|extension| extension != "jsonl") {
        return Err("query_unsupported_source");
    }
    let selected = turn.map(|value| value.into_string().map_err(|_| "query_invalid_turn_id"))
        .transpose()?;
    if selected.as_deref().is_some_and(|value| !safe_id(value)) { return Err("query_invalid_turn_id"); }
    let mut file = File::open(&path).map_err(|_| "query_source_unreadable")?;
    let metadata = file.metadata().map_err(|_| "query_source_unreadable")?;
    if !metadata.is_file() { return Err("query_invalid_source"); }
    let length = metadata.len();
    let limit = limit.map(|value| value.to_str().ok_or("query_invalid_byte_limit")?
        .parse::<u64>().map_err(|_| "query_invalid_byte_limit")).transpose()?.unwrap_or(length);
    if limit > length || limit > MAX_SAFE_INTEGER as u64 || usize::try_from(limit).is_err() {
        return Err("query_invalid_byte_limit");
    }
    let mut bytes = Vec::new();
    file.by_ref().take(limit).read_to_end(&mut bytes).map_err(|_| "query_source_unreadable")?;
    if bytes.len() as u64 != limit { return Err("query_source_changed"); }
    snapshot(&path, &bytes, selected.as_deref())
}

fn snapshot(path: &Path, bytes: &[u8], requested: Option<&str>) -> Result<Value> {
    let filename_id = crate::session_index::rollout_thread_id(path).ok_or("query_invalid_filename_id")?;
    let complete = bytes.iter().rposition(|byte| *byte == b'\n').map_or(0, |index| index + 1);
    let text = std::str::from_utf8(&bytes[..complete]).map_err(|_| "query_invalid_utf8")?;
    let trailing_partial = complete != bytes.len();
    let mut session_id = None;
    let (mut parent, mut agent_path) = (None, Some("/root".to_string()));
    let mut current: Option<String> = None;
    let mut turns = BTreeMap::<String, Turn>::new();
    let mut order = Vec::new();
    let mut total = [0_i64; 5];
    let mut counting = QueryUsageCounter::default();
    let mut complete_lines = 0_u64;
    let mut call_turns = BTreeMap::<String, (String, String)>::new();

    for line in text.lines() {
        complete_lines += 1;
        if line.trim().is_empty() { continue; }
        let entry: Value = serde_json::from_str(line).map_err(|_| "query_malformed_record")?;
        if !entry.is_object() { return Err("query_malformed_record"); }
        let kind = entry.get("type").and_then(Value::as_str).ok_or("query_incomplete_record")?;
        let payload = entry.get("payload").ok_or("query_incomplete_record")?;
        if !payload.is_object() { return Err("query_incomplete_record"); }
        if kind == "session_meta" {
            if session_id.is_some() || current.is_some() { return Err("query_ambiguous_session_metadata"); }
            let id = payload.get("id").and_then(Value::as_str).ok_or("query_missing_session_id")?;
            if !uuid(id) || !id.eq_ignore_ascii_case(filename_id) { return Err("query_session_id_mismatch"); }
            session_id = Some(id.to_ascii_lowercase());
            if let Some(spawn) = payload.pointer("/source/subagent/thread_spawn") {
                if !spawn.is_object() { return Err("query_invalid_lineage"); }
                if let Some(value) = spawn.get("parent_thread_id") {
                    let value = value.as_str().filter(|value| uuid(value)).ok_or("query_invalid_lineage")?;
                    if value.eq_ignore_ascii_case(id) { return Err("query_invalid_lineage"); }
                    parent = Some(value.to_ascii_lowercase());
                }
                agent_path = spawn.get("agent_path").map(|value|
                    value.as_str().filter(|value| safe_id(value) && value.starts_with('/'))
                        .map(ToString::to_string).ok_or("query_invalid_lineage")).transpose()?;
            }
            continue;
        }
        if session_id.is_none() { return Err("query_missing_session_id"); }
        let event_kind = if matches!(kind, "event_msg" | "response_item") {
            payload.get("type").and_then(Value::as_str).ok_or("query_incomplete_record")?
        } else { kind };
        let explicit = explicit_id(&entry, payload)?;
        let starts = kind == "turn_context" || event_kind == "task_started";
        if starts && explicit.is_none() { return Err("query_missing_turn_id"); }
        if let Some(id) = explicit {
            if current.as_deref() != Some(&id) {
                if !starts { return Err("query_conflicting_turn_id"); }
                if turns.contains_key(&id) { return Err("query_noncontiguous_turn_id"); }
                let started = timestamp(&entry)?;
                turns.insert(id.clone(), Turn { before:total, through:total, status:"active",
                    started, completed:None, calls:BTreeMap::new(), usage_recorded:false });
                order.push(id.clone());
                current = Some(id);
            }
        }
        // Every countable record must have explicit turn context. No time guesses.
        let id = current.as_ref().ok_or("query_missing_turn_id")?;
        let turn = turns.get_mut(id).ok_or("query_missing_turn_id")?;
        if starts && turn.status != "active" { return Err("query_record_after_completion"); }
        if matches!(event_kind, "task_complete" | "task_completed" | "turn_aborted" | "task_failed") {
            let state = match event_kind { "turn_aborted" => "interrupted", "task_failed" => "failed", _ => "completed" };
            if turn.status != "active" && turn.status != state { return Err("query_conflicting_completion"); }
            turn.status = state;
            turn.completed = Some(timestamp(&entry)?);
        }
        if kind == "event_msg" && event_kind == "token_count" {
            timestamp(&entry)?;
            if let Some(info) = payload.get("info") {
                if !info.is_null() {
                    let usage = counting.ingest(info)?;
                    // Lifecycle completion does not prove that numeric telemetry
                    // was recorded. An explicit zero object does; absence does not.
                    let known = ["input_tokens", "cached_input_tokens", "cache_read_input_tokens",
                        "output_tokens", "reasoning_output_tokens", "total_tokens"];
                    turn.usage_recorded |= ["last_token_usage", "total_token_usage"].iter()
                        .filter_map(|key| info.get(key)).any(|usage|
                            known.iter().any(|key| usage.get(key).is_some()));
                    if let Some(usage) = usage {
                        for index in 0..5 {
                            total[index] = total[index].checked_add(usage[index]).ok_or("query_numeric_overflow")?;
                            if total[index] > MAX_SAFE_INTEGER { return Err("query_unsafe_integer"); }
                        }
                        turn.through = total;
                    }
                }
            }
        }
        if kind == "response_item" && matches!(event_kind, "function_call" | "custom_tool_call") {
            let name = payload.get("name").and_then(Value::as_str).ok_or("query_incomplete_tool_call")?;
            if let Some(dispatch_kind) = delegation_kind(name) {
                let call = payload.get("call_id").and_then(Value::as_str)
                    .filter(|value| safe_id(value)).ok_or("query_invalid_dispatch_id")?;
                if call_turns.get(call).is_some_and(|previous| previous != &(id.clone(), dispatch_kind.to_string())) {
                    return Err("query_conflicting_dispatch_id");
                }
                call_turns.insert(call.to_string(), (id.clone(), dispatch_kind.to_string()));
                turn.calls.insert(call.to_string(), dispatch_kind.to_string());
            }
        }
    }
    let id = requested.map(ToString::to_string).or_else(|| order.last().cloned()).ok_or("query_missing_turn_id")?;
    let turn = turns.get(&id).ok_or("query_turn_not_found")?;
    if turn.status == "completed" && !turn.usage_recorded { return Err("query_missing_token_usage"); }
    Ok(json!({"schemaVersion":1,"sessionId":session_id.ok_or("query_missing_session_id")?,
        "parentSessionId":parent,"agentPath":agent_path,"turnId":id,"turnStatus":turn.status,
        "startedAt":turn.started,"completedAt":turn.completed,
        "beforeTurn":counters(turn.before),"throughTurn":counters(turn.through),
        "readBytes":bytes.len(),"completeLines":complete_lines,"trailingPartial":trailing_partial,
        "delegationCalls":turn.calls.iter().map(|(id,kind)| json!({"id":id,"kind":kind})).collect::<Vec<_>>() }))
}
