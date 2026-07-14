use graphblocks_schema::CanonicalJsonError;
use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn canonical_json(value: &Value) -> String {
    graphblocks_schema::canonical_json(value)
}

pub fn try_canonical_json(value: &Value) -> Result<String, CanonicalJsonError> {
    graphblocks_schema::try_canonical_json(value)
}

pub fn canonical_hash(value: &Value) -> String {
    try_canonical_hash(value).expect("value must satisfy canonical JSON depth limits")
}

pub fn try_canonical_hash(value: &Value) -> Result<String, CanonicalJsonError> {
    const HEX: &[u8; 16] = b"0123456789abcdef";

    let digest = Sha256::digest(try_canonical_json(value)?.as_bytes());
    let mut output = String::with_capacity("sha256:".len() + digest.len() * 2);
    output.push_str("sha256:");
    for byte in digest {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    Ok(output)
}
