use std::{env, fs, path::PathBuf};

fn replace_once(source: &str, from: &str, to: &str) -> String {
    assert_eq!(source.matches(from).count(), 1, "Pinned patch anchor changed: {from}");
    source.replacen(from, to, 1)
}

fn main() {
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR"));
    for name in ["pricing", "db", "project_sessions", "session_replay", "scanner"] {
        println!("cargo:rerun-if-changed=upstream/{name}.rs");
    }
    println!("cargo:rerun-if-changed=src/query_counting.rs");
    // Expose only a checked numeric adapter from inside the unchanged scanner
    // module. The actual normalize/subtract/convert operations are upstream's.
    let mut scanner = fs::read_to_string("upstream/scanner.rs").expect("Pinned scanner source");
    scanner.push_str("\n");
    scanner.push_str(&fs::read_to_string("src/query_counting.rs").expect("Query counting adapter"));
    fs::write(out.join("scanner_query.rs"), scanner).expect("numeric scanner adapter");
    let pricing = fs::read_to_string("upstream/pricing.rs").expect("Pinned pricing source");
    let start = pricing.find("fn load_remote_pricing() -> Result<BTreeMap<String, LiteLlmModelPricing>, String> {").expect("pricing loader");
    let end = pricing[start..].find("\nfn filter_chat_pricing(").expect("pricing loader boundary") + start;
    let mut offline_pricing = pricing[..start].to_string();
    offline_pricing.push_str("fn load_remote_pricing() -> Result<BTreeMap<String, LiteLlmModelPricing>, String> {\n    Err(\"Network pricing is disabled in codex-usage-offline\".to_string())\n}\n");
    offline_pricing.push_str(&pricing[end..]);
    fs::write(out.join("pricing.rs"), offline_pricing).expect("offline pricing");

    // URL parsing is a pure operation. Use the same url::Url type directly,
    // without linking reqwest or its HTTP/TLS dependency graph.
    let replay = fs::read_to_string("upstream/session_replay.rs").expect("Pinned replay source");
    assert_eq!(replay.matches("reqwest::Url::").count(), 2);
    let mut replay = replay.replace("reqwest::Url::", "url::Url::");
    for fixture in ["clock-sleep.json", "structured-command-responses.json", "mixed-orchestration.json"] {
        let original = format!("\"../../e2e/fixtures/{fixture}\"");
        let relocated = format!("concat!(env!(\"CARGO_MANIFEST_DIR\"), \"/upstream/e2e/fixtures/{fixture}\")");
        replay = replace_once(&replay, &original, &relocated);
    }
    fs::write(out.join("session_replay.rs"), replay).expect("URL-only replay");

    // The upstream parser and every accounting operation remain unchanged.
    // Redact only persistence arguments, before SQLite receives any title.
    let db = fs::read_to_string("upstream/db.rs").expect("Pinned database source");
    let db = replace_once(&db, "                    rollup.prompt_title,", "                    Option::<String>::None,");
    fs::write(out.join("db_offline.rs"), db).expect("private rollup persistence");

    let project = fs::read_to_string("upstream/project_sessions.rs").expect("Pinned project source");
    let project = replace_once(&project,
        "            serde_json::to_string(&detail).map_err(|error| error.to_string())?,\n            search.join(\"\\n\").to_lowercase()",
        "            serde_json::to_string(&serde_json::json!({\n                \"agentSessionId\": detail.agent_session_id,\n                \"parentSessionId\": detail.parent_session_id,\n                \"agentPath\": detail.agent_path,\n                \"agentNickname\": detail.agent_nickname,\n                \"agentRole\": detail.agent_role\n            })).map_err(|error| error.to_string())?,\n            String::new()");
    fs::write(out.join("project_sessions_offline.rs"), project).expect("private index persistence");
}
