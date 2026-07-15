use graphblocks_schema::{parse_duration_milliseconds, parse_duration_seconds};
use serde_json::json;

#[test]
fn duration_parser_supports_fractional_scientific_and_day_values() {
    for (value, expected) in [
        (json!("5e-1ms"), 1),
        (json!("1.5s"), 1_500),
        (json!("1d"), 86_400_000),
        (json!(0.0005), 1),
        (json!("1 s"), 1_000),
        (json!("1e-1000ms"), 1),
        (json!(u64::MAX), u64::MAX),
        (json!("18446744073709551615ms"), u64::MAX),
        (json!("1.8446744073709551615e19ms"), u64::MAX),
    ] {
        assert_eq!(
            parse_duration_milliseconds(&value),
            Some(expected),
            "{value}"
        );
    }
    assert_eq!(parse_duration_seconds(&json!("1.5e0d")), Some(129_600.0));
}

#[test]
fn duration_parser_rejects_zero_non_finite_and_u64_overflow() {
    for value in [
        json!(0),
        json!("0.0ms"),
        json!("nan"),
        json!("inf"),
        json!("18446744073709551616ms"),
        json!("1.8446744073709551616e19ms"),
    ] {
        assert_eq!(parse_duration_milliseconds(&value), None, "{value}");
    }
}
