#![doc = include_str!("../README.md")]
#![forbid(unsafe_code)]

/// The complete and only supported public surface of this reserved-name crate.
///
/// This notice is informational. The crate contains no schema, compiler,
/// runtime, or compatibility API.
pub const RESERVED_PACKAGE_NOTICE: &str = include_str!("../RESERVED_PACKAGE_NOTICE.txt");

#[cfg(test)]
mod tests {
    use super::RESERVED_PACKAGE_NOTICE;

    #[test]
    fn exported_notice_is_explicit_and_actionable() {
        assert_eq!(
            RESERVED_PACKAGE_NOTICE.trim(),
            "RESERVED PACKAGE: the crates.io package `graphblocks` contains no supported Rust API. Do not depend on it. The supported GraphBlocks distribution is the Python package at https://pypi.org/project/graphblocks/."
        );
    }
}
