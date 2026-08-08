use std::collections::BTreeSet;
use std::error::Error;

use graphblocks_protocol::{
    MAX_NATIVE_BINDING_IMPLEMENTATION_LENGTH, MAX_NATIVE_BINDING_IMPLEMENTATION_VERSION_LENGTH,
    NATIVE_BINDING_PROTOCOL_VERSION, NATIVE_CAPABILITY_APPLICATION_PROTOCOL,
    NATIVE_CAPABILITY_GRAPH_COMPILER, NATIVE_CAPABILITY_LOCAL_RUNTIME,
    NATIVE_CAPABILITY_WORKER_PROTOCOL, NativeBindingAdvertisement, NativeBindingAdvertisementError,
    NativeBindingPolicy, validate_native_binding_advertisement,
};
use serde_json::json;

#[test]
fn native_binding_advertisement_is_canonical_and_versioned() -> Result<(), Box<dyn Error>> {
    let advertisement = NativeBindingAdvertisement::new(
        "graphblocks-python",
        "0.1.0",
        [
            NATIVE_CAPABILITY_WORKER_PROTOCOL,
            NATIVE_CAPABILITY_APPLICATION_PROTOCOL,
            NATIVE_CAPABILITY_GRAPH_COMPILER,
            NATIVE_CAPABILITY_LOCAL_RUNTIME,
        ],
    )?;

    assert_eq!(
        serde_json::to_value(&advertisement)?,
        json!({
            "bindingProtocolVersion": NATIVE_BINDING_PROTOCOL_VERSION,
            "implementation": "graphblocks-python",
            "implementationVersion": "0.1.0",
            "capabilities": [
                NATIVE_CAPABILITY_GRAPH_COMPILER,
                NATIVE_CAPABILITY_APPLICATION_PROTOCOL,
                NATIVE_CAPABILITY_WORKER_PROTOCOL,
                NATIVE_CAPABILITY_LOCAL_RUNTIME,
            ],
        })
    );
    Ok(())
}

#[test]
fn native_binding_handshake_rejects_an_unsupported_protocol_version() {
    let advertisement = NativeBindingAdvertisement {
        binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION + 1,
        implementation: "graphblocks-python".to_owned(),
        implementation_version: "0.1.0".to_owned(),
        capabilities: vec![NATIVE_CAPABILITY_GRAPH_COMPILER.to_owned()],
    };

    assert_eq!(
        validate_native_binding_advertisement(&NativeBindingPolicy::current(), &advertisement,),
        Err(
            NativeBindingAdvertisementError::IncompatibleProtocolVersion {
                expected: NATIVE_BINDING_PROTOCOL_VERSION,
                actual: NATIVE_BINDING_PROTOCOL_VERSION + 1,
            }
        )
    );
}

#[test]
fn native_binding_handshake_rejects_missing_required_capabilities() {
    let advertisement = NativeBindingAdvertisement {
        binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION,
        implementation: "graphblocks-python".to_owned(),
        implementation_version: "0.1.0".to_owned(),
        capabilities: vec![NATIVE_CAPABILITY_GRAPH_COMPILER.to_owned()],
    };
    let policy =
        NativeBindingPolicy::current().require_capability(NATIVE_CAPABILITY_WORKER_PROTOCOL);

    assert_eq!(
        validate_native_binding_advertisement(&policy, &advertisement),
        Err(NativeBindingAdvertisementError::MissingRequiredCapability {
            capability: NATIVE_CAPABILITY_WORKER_PROTOCOL.to_owned(),
        })
    );
}

#[test]
fn native_binding_handshake_requires_local_runtime_semantics_when_claimed() {
    let advertisement = NativeBindingAdvertisement {
        binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION,
        implementation: "graphblocks-python".to_owned(),
        implementation_version: "0.1.0".to_owned(),
        capabilities: vec![NATIVE_CAPABILITY_GRAPH_COMPILER.to_owned()],
    };
    let policy = NativeBindingPolicy::current().require_capability(NATIVE_CAPABILITY_LOCAL_RUNTIME);

    assert_eq!(
        validate_native_binding_advertisement(&policy, &advertisement),
        Err(NativeBindingAdvertisementError::MissingRequiredCapability {
            capability: NATIVE_CAPABILITY_LOCAL_RUNTIME.to_owned(),
        })
    );
}

#[test]
fn native_binding_handshake_preserves_future_optional_capabilities() -> Result<(), Box<dyn Error>> {
    let advertisement = NativeBindingAdvertisement::new(
        "graphblocks-python",
        "0.1.0",
        [
            NATIVE_CAPABILITY_GRAPH_COMPILER,
            NATIVE_CAPABILITY_APPLICATION_PROTOCOL,
            NATIVE_CAPABILITY_WORKER_PROTOCOL,
            "vendor.future.v1",
        ],
    )?;
    let policy =
        NativeBindingPolicy::current().require_capability(NATIVE_CAPABILITY_GRAPH_COMPILER);

    validate_native_binding_advertisement(&policy, &advertisement)?;
    assert_eq!(
        advertisement.capabilities.last().map(String::as_str),
        Some("vendor.future.v1")
    );
    Ok(())
}

#[test]
fn native_binding_handshake_rejects_noncanonical_or_unknown_wire_fields() {
    let duplicate = NativeBindingAdvertisement {
        binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION,
        implementation: "graphblocks-python".to_owned(),
        implementation_version: "0.1.0".to_owned(),
        capabilities: vec![
            NATIVE_CAPABILITY_GRAPH_COMPILER.to_owned(),
            NATIVE_CAPABILITY_GRAPH_COMPILER.to_owned(),
        ],
    };

    assert_eq!(
        validate_native_binding_advertisement(
            &NativeBindingPolicy {
                binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION,
                required_capabilities: BTreeSet::new(),
            },
            &duplicate,
        ),
        Err(NativeBindingAdvertisementError::NonCanonicalCapabilities)
    );
    let invalid_capability = NativeBindingAdvertisement {
        binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION,
        implementation: "graphblocks-python".to_owned(),
        implementation_version: "0.1.0".to_owned(),
        capabilities: vec![" protocol.worker.v1".to_owned()],
    };
    assert_eq!(
        validate_native_binding_advertisement(&NativeBindingPolicy::current(), &invalid_capability,),
        Err(NativeBindingAdvertisementError::InvalidCapability)
    );
    assert!(
        serde_json::from_value::<NativeBindingAdvertisement>(json!({
            "bindingProtocolVersion": NATIVE_BINDING_PROTOCOL_VERSION,
            "implementation": "graphblocks-python",
            "implementationVersion": "0.1.0",
            "capabilities": [NATIVE_CAPABILITY_GRAPH_COMPILER],
            "unexpected": true,
        }))
        .is_err()
    );
}

#[test]
fn native_binding_handshake_rejects_noncanonical_or_unbounded_identity_fields() {
    let whitespace_version = NativeBindingAdvertisement {
        binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION,
        implementation: "graphblocks-python".to_owned(),
        implementation_version: " 0.1.0".to_owned(),
        capabilities: vec![NATIVE_CAPABILITY_GRAPH_COMPILER.to_owned()],
    };
    assert_eq!(
        validate_native_binding_advertisement(&NativeBindingPolicy::current(), &whitespace_version,),
        Err(NativeBindingAdvertisementError::InvalidImplementationVersion)
    );

    let oversized_implementation = NativeBindingAdvertisement {
        binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION,
        implementation: "x".repeat(MAX_NATIVE_BINDING_IMPLEMENTATION_LENGTH + 1),
        implementation_version: "0.1.0".to_owned(),
        capabilities: vec![NATIVE_CAPABILITY_GRAPH_COMPILER.to_owned()],
    };
    assert_eq!(
        validate_native_binding_advertisement(
            &NativeBindingPolicy::current(),
            &oversized_implementation,
        ),
        Err(NativeBindingAdvertisementError::InvalidImplementation)
    );

    let oversized_version = NativeBindingAdvertisement {
        binding_protocol_version: NATIVE_BINDING_PROTOCOL_VERSION,
        implementation: "graphblocks-python".to_owned(),
        implementation_version: "1".repeat(MAX_NATIVE_BINDING_IMPLEMENTATION_VERSION_LENGTH + 1),
        capabilities: vec![NATIVE_CAPABILITY_GRAPH_COMPILER.to_owned()],
    };
    assert_eq!(
        validate_native_binding_advertisement(&NativeBindingPolicy::current(), &oversized_version,),
        Err(NativeBindingAdvertisementError::InvalidImplementationVersion)
    );
}
