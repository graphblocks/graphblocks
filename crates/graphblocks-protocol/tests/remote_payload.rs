use std::collections::BTreeMap;

use graphblocks_protocol::{
    ArtifactRef, RemotePayload, RemotePayloadError, RemotePayloadLimits, validate_remote_payload,
};
use serde_json::{Value, json};

#[test]
fn artifact_ref_round_trips_with_canonical_wire_field_names() -> Result<(), serde_json::Error> {
    let artifact = ArtifactRef {
        artifact_id: "artifact-000001".to_owned(),
        uri: "s3://graphblocks/documents/source.pdf".to_owned(),
        media_type: Some("application/pdf".to_owned()),
        size_bytes: Some(10_000_000),
        checksum: Some("sha256:document".to_owned()),
        etag: Some("etag-1".to_owned()),
        version: Some("v1".to_owned()),
        filename: Some("source.pdf".to_owned()),
        metadata: BTreeMap::from([("tenant".to_owned(), "acme".to_owned())]),
    };

    let encoded = serde_json::to_value(&artifact)?;

    assert_eq!(encoded["artifactId"], json!("artifact-000001"));
    assert_eq!(encoded["sizeBytes"], json!(10_000_000u64));
    assert_eq!(encoded["metadata"]["tenant"], json!("acme"));
    assert_eq!(serde_json::from_value::<ArtifactRef>(encoded)?, artifact);
    Ok(())
}

#[test]
fn remote_payload_rejects_oversized_inline_json() {
    let value = json!({"body": "this inline payload is too large"});
    let actual_inline_bytes = serde_json::to_vec(&value)
        .expect("json should encode")
        .len();
    let payload = RemotePayload::Inline {
        schema: "graphblocks.ai/Message@1".to_owned(),
        value,
    };

    assert_eq!(
        validate_remote_payload(
            &payload,
            &RemotePayloadLimits {
                max_inline_bytes: 8
            }
        ),
        Err(RemotePayloadError::OversizedInlinePayload {
            max_inline_bytes: 8,
            actual_inline_bytes,
        }),
    );
}

#[test]
fn remote_payload_rejects_blank_schema() {
    let payload = RemotePayload::Inline {
        schema: " ".to_owned(),
        value: json!({"body": "hello"}),
    };

    assert_eq!(
        validate_remote_payload(
            &payload,
            &RemotePayloadLimits {
                max_inline_bytes: 128
            }
        ),
        Err(RemotePayloadError::InvalidSchema),
    );
}

#[test]
fn remote_payload_rejects_inline_json_beyond_canonical_depth() {
    let mut value = Value::Null;
    for _ in 0..65 {
        value = Value::Array(vec![value]);
    }
    let payload = RemotePayload::Inline {
        schema: "graphblocks.ai/Message@1".to_owned(),
        value,
    };

    assert_eq!(
        validate_remote_payload(
            &payload,
            &RemotePayloadLimits {
                max_inline_bytes: usize::MAX,
            },
        ),
        Err(RemotePayloadError::InlineJsonEncoding),
    );
}

#[test]
fn remote_payload_rejects_unknown_variant_and_artifact_fields() {
    let variant_error = serde_json::from_value::<RemotePayload>(json!({
        "mode": "inline",
        "schema": "graphblocks.ai/Message@1",
        "value": {},
        "artifact": {}
    }))
    .expect_err("fields from another payload mode must not be discarded");
    assert!(
        variant_error.to_string().contains("unknown field"),
        "{variant_error}"
    );

    let artifact_error = serde_json::from_value::<RemotePayload>(json!({
        "mode": "artifact_ref",
        "schema": "graphblocks.ai/ArtifactRef@1",
        "artifact": {
            "artifactId": "artifact-000001",
            "uri": "s3://graphblocks/documents/source.pdf",
            "metadata": {},
            "inlineValue": {}
        }
    }))
    .expect_err("unknown artifact fields must not be discarded");
    assert!(
        artifact_error.to_string().contains("unknown field"),
        "{artifact_error}"
    );
}

#[test]
fn remote_payload_rejects_duplicate_mode_field() {
    let error = serde_json::from_str::<RemotePayload>(
        r#"{"mode":"inline","mode":"artifact_ref","schema":"graphblocks.ai/Message@1","value":{}}"#,
    )
    .expect_err("duplicate mode fields must be rejected");

    assert!(error.to_string().contains("duplicate field"), "{error}");
}

#[test]
fn remote_payload_allows_large_artifact_by_reference() -> Result<(), serde_json::Error> {
    let payload = RemotePayload::ArtifactRef {
        schema: "graphblocks.ai/ArtifactRef@1".to_owned(),
        artifact: ArtifactRef {
            artifact_id: "artifact-000001".to_owned(),
            uri: "s3://graphblocks/documents/source.pdf".to_owned(),
            media_type: Some("application/pdf".to_owned()),
            size_bytes: Some(10_000_000),
            checksum: Some("sha256:document".to_owned()),
            etag: None,
            version: None,
            filename: Some("source.pdf".to_owned()),
            metadata: BTreeMap::new(),
        },
    };

    let encoded = serde_json::to_value(&payload)?;

    assert_eq!(encoded["mode"], json!("artifact_ref"));
    assert_eq!(encoded["artifact"]["artifactId"], json!("artifact-000001"));
    assert_eq!(
        validate_remote_payload(
            &payload,
            &RemotePayloadLimits {
                max_inline_bytes: 8
            }
        ),
        Ok(()),
    );
    Ok(())
}

#[test]
fn remote_payload_rejects_blank_artifact_reference_fields() {
    let mut payload = RemotePayload::ArtifactRef {
        schema: "graphblocks.ai/ArtifactRef@1".to_owned(),
        artifact: ArtifactRef {
            artifact_id: " ".to_owned(),
            uri: "s3://graphblocks/documents/source.pdf".to_owned(),
            media_type: None,
            size_bytes: None,
            checksum: None,
            etag: None,
            version: None,
            filename: None,
            metadata: BTreeMap::new(),
        },
    };

    assert_eq!(
        validate_remote_payload(
            &payload,
            &RemotePayloadLimits {
                max_inline_bytes: 8
            }
        ),
        Err(RemotePayloadError::InvalidArtifactRef {
            field: "artifact_id".to_owned(),
        }),
    );

    if let RemotePayload::ArtifactRef { artifact, .. } = &mut payload {
        artifact.artifact_id = "artifact-000001".to_owned();
        artifact.uri = " ".to_owned();
    }

    assert_eq!(
        validate_remote_payload(
            &payload,
            &RemotePayloadLimits {
                max_inline_bytes: 8
            }
        ),
        Err(RemotePayloadError::InvalidArtifactRef {
            field: "uri".to_owned(),
        }),
    );
}
