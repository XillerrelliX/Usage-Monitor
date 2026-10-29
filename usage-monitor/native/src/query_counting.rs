// Appended by build.rs inside the unchanged upstream scanner module.
// This adapter validates arithmetic before invoking the canonical functions;
// it does not reproduce their counting, subset, or cumulative-reset rules.
#[derive(Default)]
pub(crate) struct QueryUsageCounter {
    previous_totals: Option<RawUsage>,
}

impl QueryUsageCounter {
    pub(crate) fn ingest(&mut self, info: &Value) -> Result<Option<[i64; 5]>, &'static str> {
        fn validate(value: Option<&Value>) -> Result<(), &'static str> {
            let Some(value) = value else { return Ok(()); };
            if !value.is_object() { return Err("query_invalid_usage"); }
            for key in ["input_tokens", "cached_input_tokens", "cache_read_input_tokens",
                        "output_tokens", "reasoning_output_tokens", "total_tokens"] {
                if let Some(number) = value.get(key) {
                    if number.as_i64().is_none_or(|number| number < 0) {
                        return Err("query_invalid_usage_number");
                    }
                }
            }
            if value.get("total_tokens").and_then(Value::as_i64).unwrap_or(0) == 0 {
                value.get("input_tokens").and_then(Value::as_i64).unwrap_or(0)
                    .checked_add(value.get("output_tokens").and_then(Value::as_i64).unwrap_or(0))
                    .ok_or("query_numeric_overflow")?;
            }
            Ok(())
        }
        if !info.is_object() { return Err("query_invalid_usage"); }
        validate(info.get("last_token_usage"))?;
        validate(info.get("total_token_usage"))?;
        let last_usage = normalize_raw_usage(info.get("last_token_usage"));
        let total_usage = normalize_raw_usage(info.get("total_token_usage"));
        let raw = last_usage.or_else(|| total_usage.as_ref()
            .map(|current| subtract_raw_usage(current, self.previous_totals.as_ref())));
        if let Some(total_usage) = total_usage { self.previous_totals = Some(total_usage); }
        let Some(raw) = raw else { return Ok(None); };
        if raw.total_tokens <= 0 {
            raw.input_tokens.checked_add(raw.output_tokens).ok_or("query_numeric_overflow")?;
        }
        let usage = convert_to_delta(&raw);
        // The canonical scanner intentionally ignores total-only telemetry.
        if usage.input_tokens == 0 && usage.cached_input_tokens == 0
            && usage.output_tokens == 0 && usage.reasoning_output_tokens == 0 {
            return Ok(None);
        }
        Ok(Some([usage.input_tokens, usage.cached_input_tokens, usage.output_tokens,
                 usage.reasoning_output_tokens, usage.total_tokens]))
    }
}
