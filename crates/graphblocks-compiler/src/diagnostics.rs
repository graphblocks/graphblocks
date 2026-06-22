#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Severity {
    Error,
    Warning,
    Info,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Diagnostic {
    pub code: String,
    pub message: String,
    pub path: String,
    pub severity: Severity,
}

impl Diagnostic {
    pub fn error(
        code: impl Into<String>,
        message: impl Into<String>,
        path: impl Into<String>,
    ) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            path: path.into(),
            severity: Severity::Error,
        }
    }
}
