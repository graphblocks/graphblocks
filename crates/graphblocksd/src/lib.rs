use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv6Addr, SocketAddr, TcpStream};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use graphblocks_protocol::{
    WORKER_PROTOCOL_VERSION, WorkerAdmissionDecision, WorkerAdmissionPolicy, WorkerAdvertisement,
    WorkerDrainError, WorkerDrainPlan, WorkerDrainPolicy, WorkerDrainTask, WorkerProtocolMessage,
    WorkerProtocolMessageKind, WorkerProtocolMessagePayload, WorkerState,
    evaluate_worker_admission,
};
use graphblocks_runtime_core::callback_delivery::{WebhookHttpRequest, WebhookHttpResponse};
use rustls::pki_types::{CertificateDer, ServerName};
use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};
use serde_json::Value;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DaemonConfig {
    pub daemon_id: String,
    pub bind_address: String,
    pub protocol_version: u16,
    pub package_lock_hash: Option<String>,
    pub max_workers: usize,
}

impl DaemonConfig {
    pub fn new(daemon_id: impl Into<String>, bind_address: impl Into<String>) -> Self {
        Self {
            daemon_id: daemon_id.into(),
            bind_address: bind_address.into(),
            protocol_version: WORKER_PROTOCOL_VERSION,
            package_lock_hash: None,
            max_workers: 1024,
        }
    }

    pub fn with_protocol_version(mut self, protocol_version: u16) -> Self {
        self.protocol_version = protocol_version;
        self
    }

    pub fn require_package_lock_hash(mut self, package_lock_hash: impl Into<String>) -> Self {
        self.package_lock_hash = Some(package_lock_hash.into());
        self
    }

    pub fn with_max_workers(mut self, max_workers: usize) -> Self {
        self.max_workers = max_workers;
        self
    }

    pub fn validate(&self) -> Result<(), DaemonConfigError> {
        if self.daemon_id.trim().is_empty()
            || self.daemon_id.trim() != self.daemon_id
            || self.daemon_id.chars().any(char::is_whitespace)
        {
            return Err(DaemonConfigError::EmptyDaemonId);
        }
        if self.bind_address.trim().is_empty()
            || self.bind_address.trim() != self.bind_address
            || self.bind_address.chars().any(char::is_whitespace)
        {
            return Err(DaemonConfigError::EmptyBindAddress);
        }
        if self.protocol_version != WORKER_PROTOCOL_VERSION {
            return Err(DaemonConfigError::UnsupportedProtocolVersion {
                expected: WORKER_PROTOCOL_VERSION,
                actual: self.protocol_version,
            });
        }
        if self.max_workers == 0 {
            return Err(DaemonConfigError::ZeroMaxWorkers);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DaemonConfigError {
    EmptyDaemonId,
    EmptyBindAddress,
    ZeroMaxWorkers,
    UnsupportedProtocolVersion { expected: u16, actual: u16 },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DaemonStatus {
    pub daemon_id: String,
    pub bind_address: String,
    pub protocol_version: u16,
    pub ready_workers: usize,
    pub saturated_workers: usize,
    pub draining_workers: usize,
    pub admitted_workers: usize,
    pub rejected_workers: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerRegistry {
    config: DaemonConfig,
    admitted_workers: BTreeMap<String, WorkerAdmissionDecision>,
    admitted_advertisements: BTreeMap<String, WorkerAdvertisement>,
    rejected_workers: usize,
}

impl WorkerRegistry {
    pub fn new(config: DaemonConfig) -> Result<Self, DaemonConfigError> {
        config.validate()?;
        Ok(Self {
            config,
            admitted_workers: BTreeMap::new(),
            admitted_advertisements: BTreeMap::new(),
            rejected_workers: 0,
        })
    }

    pub fn admit_worker(&mut self, advertisement: WorkerAdvertisement) -> WorkerAdmissionDecision {
        let policy = WorkerAdmissionPolicy {
            protocol_version: self.config.protocol_version,
            package_lock_hash: self.config.package_lock_hash.clone(),
            required_block: None,
        };
        let mut decision = evaluate_worker_admission(&policy, &advertisement);
        let is_known_worker = self.admitted_workers.contains_key(&decision.worker_id);
        if decision.admitted
            && !is_known_worker
            && self.admitted_workers.len() >= self.config.max_workers
        {
            decision.admitted = false;
            decision
                .reason_codes
                .push("daemon.max_workers_exceeded".to_owned());
        }

        if decision.admitted {
            self.admitted_workers
                .insert(decision.worker_id.clone(), decision.clone());
            self.admitted_advertisements
                .insert(decision.worker_id.clone(), advertisement);
        } else {
            if is_known_worker {
                self.admitted_workers.remove(&decision.worker_id);
                self.admitted_advertisements.remove(&decision.worker_id);
            }
            self.rejected_workers += 1;
        }
        decision
    }

    pub fn admit_worker_message(
        &mut self,
        message: WorkerProtocolMessage,
        response_message_id: impl Into<String>,
        response_sequence: u64,
    ) -> Result<WorkerProtocolMessage, WorkerRegistryError> {
        if message.protocol_version != WORKER_PROTOCOL_VERSION {
            return Err(WorkerRegistryError::IncompatibleMessageProtocolVersion {
                expected: WORKER_PROTOCOL_VERSION,
                actual: message.protocol_version,
            });
        }
        if message.message_id.trim().is_empty() {
            return Err(WorkerRegistryError::EmptyMessageId);
        }
        if message
            .correlation_id
            .as_ref()
            .is_some_and(|correlation_id| correlation_id.trim().is_empty())
        {
            return Err(WorkerRegistryError::EmptyCorrelationId);
        }
        if message
            .causation_id
            .as_ref()
            .is_some_and(|causation_id| causation_id.trim().is_empty())
        {
            return Err(WorkerRegistryError::EmptyCausationId);
        }
        let payload_kind = message.payload.kind();
        if message.kind != payload_kind {
            return Err(WorkerRegistryError::KindPayloadMismatch {
                kind: message.kind,
                payload_kind,
            });
        }
        let correlation_id = message.correlation_id.clone();
        let causation_id = message.message_id.clone();
        let WorkerProtocolMessagePayload::Advertisement(advertisement) = message.payload else {
            return Err(WorkerRegistryError::UnexpectedWorkerMessageKind { kind: message.kind });
        };
        let decision = self.admit_worker(advertisement);
        let mut response = WorkerProtocolMessage::admission_decision(
            response_message_id,
            response_sequence,
            decision,
        )
        .with_causation_id(causation_id);
        if let Some(correlation_id) = correlation_id {
            response = response.with_correlation_id(correlation_id);
        }
        Ok(response)
    }

    pub fn admit_worker_message_wire_value(
        &mut self,
        message: &Value,
        response_message_id: impl Into<String>,
        response_sequence: u64,
    ) -> Result<WorkerProtocolMessage, WorkerRegistryError> {
        let message = parse_worker_advertisement_message_wire_value(message)?;
        self.admit_worker_message(message, response_message_id, response_sequence)
    }

    pub fn ready_worker_ids(&self) -> Vec<String> {
        self.worker_ids_by_state(WorkerState::Ready)
    }

    pub fn worker_ids_by_state(&self, state: WorkerState) -> Vec<String> {
        self.admitted_workers
            .values()
            .filter(|decision| decision.state == state)
            .map(|decision| decision.worker_id.clone())
            .collect()
    }

    pub fn status(&self) -> DaemonStatus {
        let ready_workers = self.worker_ids_by_state(WorkerState::Ready).len();
        let saturated_workers = self.worker_ids_by_state(WorkerState::Saturated).len();
        let draining_workers = self.worker_ids_by_state(WorkerState::Draining).len();
        DaemonStatus {
            daemon_id: self.config.daemon_id.clone(),
            bind_address: self.config.bind_address.clone(),
            protocol_version: self.config.protocol_version,
            ready_workers,
            saturated_workers,
            draining_workers,
            admitted_workers: self.admitted_workers.len(),
            rejected_workers: self.rejected_workers,
        }
    }

    pub fn drain_worker<I>(
        &mut self,
        worker_id: impl AsRef<str>,
        policy: &WorkerDrainPolicy,
        tasks: I,
        drain_started_at_unix_ms: u64,
        now_unix_ms: u64,
    ) -> Result<WorkerDrainPlan, WorkerRegistryError>
    where
        I: IntoIterator<Item = WorkerDrainTask>,
    {
        let worker_id = worker_id.as_ref();
        let Some(worker) = self.admitted_advertisements.get(worker_id).cloned() else {
            return Err(WorkerRegistryError::UnknownWorker {
                worker_id: worker_id.to_owned(),
            });
        };
        let plan = WorkerDrainPlan::for_worker(
            &worker,
            policy,
            tasks,
            drain_started_at_unix_ms,
            now_unix_ms,
        )
        .map_err(|source| WorkerRegistryError::DrainPlan { source })?;
        if let Some(decision) = self.admitted_workers.get_mut(worker_id) {
            decision.state = WorkerState::Draining;
        }
        if let Some(advertisement) = self.admitted_advertisements.get_mut(worker_id) {
            advertisement.state = WorkerState::Draining;
        }
        Ok(plan)
    }
}

pub trait WebhookHttpClient {
    fn send(
        &mut self,
        request: WebhookHttpRequest,
        validated_addresses: &[IpAddr],
    ) -> Result<WebhookHttpResponse, WebhookHttpClientError>;
}

#[derive(Clone, Debug)]
pub struct StdWebhookHttpClient {
    timeout: Duration,
    tls_config: Arc<ClientConfig>,
    tls_roots: TlsRoots,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum TlsRoots {
    WebPki,
    Custom(Vec<Vec<u8>>),
}

impl PartialEq for StdWebhookHttpClient {
    fn eq(&self, other: &Self) -> bool {
        self.timeout == other.timeout && self.tls_roots == other.tls_roots
    }
}

impl Eq for StdWebhookHttpClient {}

impl StdWebhookHttpClient {
    pub fn new(timeout: Duration) -> Self {
        let roots = webpki_roots::TLS_SERVER_ROOTS.iter().cloned().collect();
        Self::with_root_store(timeout, roots, TlsRoots::WebPki)
    }

    pub fn with_root_certificates(
        timeout: Duration,
        certificates: impl IntoIterator<Item = Vec<u8>>,
    ) -> Result<Self, WebhookHttpClientError> {
        let certificates = certificates.into_iter().collect::<Vec<_>>();
        let mut roots = RootCertStore::empty();
        for certificate in &certificates {
            roots
                .add(CertificateDer::from(certificate.clone()))
                .map_err(|error| WebhookHttpClientError::Tls(error.to_string()))?;
        }
        Ok(Self::with_root_store(
            timeout,
            roots,
            TlsRoots::Custom(certificates),
        ))
    }

    fn with_root_store(timeout: Duration, roots: RootCertStore, tls_roots: TlsRoots) -> Self {
        let tls_config = ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();
        Self {
            timeout,
            tls_config: Arc::new(tls_config),
            tls_roots,
        }
    }
}

impl WebhookHttpClient for StdWebhookHttpClient {
    fn send(
        &mut self,
        request: WebhookHttpRequest,
        validated_addresses: &[IpAddr],
    ) -> Result<WebhookHttpResponse, WebhookHttpClientError> {
        if request.method != "POST" {
            return Err(WebhookHttpClientError::UnsupportedMethod(request.method));
        }
        let endpoint = parse_http_url(&request.url)?;
        let body = request.canonical_body();
        let mut has_content_type = false;
        let mut has_content_length = false;
        let mut has_connection = false;
        for (name, value) in &request.headers {
            validate_header(name, value)?;
            if name.eq_ignore_ascii_case("content-type") {
                has_content_type = true;
            } else if name.eq_ignore_ascii_case("content-length") {
                has_content_length = true;
            } else if name.eq_ignore_ascii_case("connection") {
                has_connection = true;
            }
        }

        let mut last_connect_error = None;
        let mut connected_stream = None;
        for address in validated_addresses {
            match TcpStream::connect_timeout(
                &SocketAddr::new(*address, endpoint.port),
                self.timeout,
            ) {
                Ok(stream) => {
                    connected_stream = Some(stream);
                    break;
                }
                Err(error) => last_connect_error = Some(error.to_string()),
            }
        }
        let mut stream = connected_stream.ok_or_else(|| {
            last_connect_error.map_or(WebhookHttpClientError::MissingValidatedAddress, |error| {
                WebhookHttpClientError::Connect(error)
            })
        })?;
        stream
            .set_read_timeout(Some(self.timeout))
            .map_err(|error| WebhookHttpClientError::Transport(error.to_string()))?;
        stream
            .set_write_timeout(Some(self.timeout))
            .map_err(|error| WebhookHttpClientError::Transport(error.to_string()))?;

        let mut wire = String::new();
        wire.push_str("POST ");
        wire.push_str(&endpoint.path_and_query);
        wire.push_str(" HTTP/1.1\r\n");
        wire.push_str("Host: ");
        wire.push_str(&endpoint.host_header);
        wire.push_str("\r\n");

        for (name, value) in &request.headers {
            wire.push_str(name);
            wire.push_str(": ");
            wire.push_str(value);
            wire.push_str("\r\n");
        }
        if !has_content_type {
            wire.push_str("Content-Type: application/json\r\n");
        }
        if !has_content_length {
            wire.push_str("Content-Length: ");
            wire.push_str(&body.len().to_string());
            wire.push_str("\r\n");
        }
        if !has_connection {
            wire.push_str("Connection: close\r\n");
        }
        wire.push_str("\r\n");
        wire.push_str(&body);

        let response = if endpoint.use_tls {
            let server_name = ServerName::try_from(endpoint.server_name.clone())
                .map_err(|_| WebhookHttpClientError::MalformedUrl)?;
            let connection = ClientConnection::new(self.tls_config.clone(), server_name)
                .map_err(|error| WebhookHttpClientError::Tls(error.to_string()))?;
            let mut stream = StreamOwned::new(connection, stream);
            exchange_http(&mut stream, &wire)?
        } else {
            exchange_http(&mut stream, &wire)?
        };
        parse_http_response(&response)
    }
}

fn exchange_http(
    stream: &mut (impl Read + Write),
    wire: &str,
) -> Result<String, WebhookHttpClientError> {
    const MAX_RESPONSE_HEADER_BYTES: usize = 64 * 1024;

    stream
        .write_all(wire.as_bytes())
        .map_err(|error| WebhookHttpClientError::Transport(error.to_string()))?;
    stream
        .flush()
        .map_err(|error| WebhookHttpClientError::Transport(error.to_string()))?;

    let mut response = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let read = stream
            .read(&mut buffer)
            .map_err(|error| WebhookHttpClientError::Transport(error.to_string()))?;
        if read == 0 {
            return Err(WebhookHttpClientError::MalformedResponse);
        }
        response.extend_from_slice(&buffer[..read]);
        if let Some(header_end) = response.windows(4).position(|window| window == b"\r\n\r\n") {
            let header_end = header_end + 4;
            if header_end > MAX_RESPONSE_HEADER_BYTES {
                return Err(WebhookHttpClientError::ResponseHeadersTooLarge);
            }
            return String::from_utf8(response[..header_end].to_vec())
                .map_err(|_| WebhookHttpClientError::MalformedResponse);
        }
        if response.len() > MAX_RESPONSE_HEADER_BYTES {
            return Err(WebhookHttpClientError::ResponseHeadersTooLarge);
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WebhookHttpClientError {
    EmptyUrl,
    UnsupportedScheme(String),
    UnsupportedMethod(String),
    MalformedUrl,
    InvalidPort,
    InvalidHeader,
    MissingValidatedAddress,
    Connect(String),
    Tls(String),
    Transport(String),
    ResponseHeadersTooLarge,
    MalformedResponse,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ParsedHttpEndpoint {
    host_header: String,
    server_name: String,
    port: u16,
    path_and_query: String,
    use_tls: bool,
}

fn parse_http_url(url: &str) -> Result<ParsedHttpEndpoint, WebhookHttpClientError> {
    if url.trim().is_empty() {
        return Err(WebhookHttpClientError::EmptyUrl);
    }
    let (rest, default_port, use_tls) = if let Some(rest) = url.strip_prefix("https://") {
        (rest, 443, true)
    } else if let Some(rest) = url.strip_prefix("http://") {
        (rest, 80, false)
    } else if let Some((scheme, _)) = url.split_once("://") {
        return Err(WebhookHttpClientError::UnsupportedScheme(scheme.to_owned()));
    } else {
        return Err(WebhookHttpClientError::MalformedUrl);
    };
    if rest.contains('#') {
        return Err(WebhookHttpClientError::MalformedUrl);
    }
    let (authority, path_and_query) = rest
        .char_indices()
        .find(|(_, character)| matches!(character, '/' | '?'))
        .map_or((rest, "/".to_owned()), |(index, character)| {
            let path_and_query = if character == '?' {
                format!("/{}", &rest[index..])
            } else {
                rest[index..].to_owned()
            };
            (&rest[..index], path_and_query)
        });
    if path_and_query.bytes().any(|byte| byte <= 32 || byte == 127) {
        return Err(WebhookHttpClientError::MalformedUrl);
    }
    if authority.is_empty() || authority.contains('@') {
        return Err(WebhookHttpClientError::MalformedUrl);
    }
    let (server_name, port) = parse_authority(authority, default_port)?;
    Ok(ParsedHttpEndpoint {
        host_header: authority.to_owned(),
        server_name,
        port,
        path_and_query,
        use_tls,
    })
}

fn parse_authority(
    authority: &str,
    default_port: u16,
) -> Result<(String, u16), WebhookHttpClientError> {
    if let Some(rest) = authority.strip_prefix('[') {
        let (host, suffix) = rest
            .split_once(']')
            .ok_or(WebhookHttpClientError::MalformedUrl)?;
        if host.contains('%') || host.parse::<Ipv6Addr>().is_err() {
            return Err(WebhookHttpClientError::MalformedUrl);
        }
        let port = if let Some(port) = suffix.strip_prefix(':') {
            parse_port(port)?
        } else if suffix.is_empty() {
            default_port
        } else {
            return Err(WebhookHttpClientError::MalformedUrl);
        };
        return Ok((host.to_owned(), port));
    }
    let (host, port) = authority
        .split_once(':')
        .map_or(Ok((authority, default_port)), |(host, port)| {
            Ok((host, parse_port(port)?))
        })?;
    if host.is_empty() || host.contains(':') {
        return Err(WebhookHttpClientError::MalformedUrl);
    }
    let host = host.trim_end_matches('.').to_ascii_lowercase();
    if !host
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.'))
    {
        return Err(WebhookHttpClientError::MalformedUrl);
    }
    if host
        .split('.')
        .any(|label| label.is_empty() || label.starts_with('-') || label.ends_with('-'))
    {
        return Err(WebhookHttpClientError::MalformedUrl);
    }
    Ok((host, port))
}

fn parse_port(port: &str) -> Result<u16, WebhookHttpClientError> {
    port.parse::<u16>()
        .map_err(|_| WebhookHttpClientError::InvalidPort)
}

fn validate_header(name: &str, value: &str) -> Result<(), WebhookHttpClientError> {
    if name.is_empty()
        || !name.bytes().all(is_http_token_byte)
        || value
            .bytes()
            .any(|byte| (byte < 32 && byte != b'\t') || byte == 127)
    {
        return Err(WebhookHttpClientError::InvalidHeader);
    }
    Ok(())
}

fn is_http_token_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(
            byte,
            b'!' | b'#'
                | b'$'
                | b'%'
                | b'&'
                | b'\''
                | b'*'
                | b'+'
                | b'-'
                | b'.'
                | b'^'
                | b'_'
                | b'`'
                | b'|'
                | b'~'
        )
}

fn parse_http_response(response: &str) -> Result<WebhookHttpResponse, WebhookHttpClientError> {
    parse_http_response_at(response, SystemTime::now())
}

fn parse_http_response_at(
    response: &str,
    now: SystemTime,
) -> Result<WebhookHttpResponse, WebhookHttpClientError> {
    let header_end = response
        .find("\r\n\r\n")
        .ok_or(WebhookHttpClientError::MalformedResponse)?;
    let mut lines = response[..header_end].split("\r\n");
    let status_line = lines
        .next()
        .ok_or(WebhookHttpClientError::MalformedResponse)?;
    if status_line
        .bytes()
        .any(|byte| byte < 32 || byte == 127 || !byte.is_ascii())
    {
        return Err(WebhookHttpClientError::MalformedResponse);
    }
    let mut status_parts = status_line.splitn(3, ' ');
    let version = status_parts
        .next()
        .ok_or(WebhookHttpClientError::MalformedResponse)?;
    if !matches!(version, "HTTP/1.0" | "HTTP/1.1") {
        return Err(WebhookHttpClientError::MalformedResponse);
    }
    let status_text = status_parts
        .next()
        .ok_or(WebhookHttpClientError::MalformedResponse)?;
    if status_text.len() != 3 || !status_text.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(WebhookHttpClientError::MalformedResponse);
    }
    let status = status_text
        .parse::<u16>()
        .map_err(|_| WebhookHttpClientError::MalformedResponse)?;
    if !(100..=599).contains(&status) {
        return Err(WebhookHttpClientError::MalformedResponse);
    }
    let mut retry_after_ms = None;
    let mut saw_retry_after = false;
    for line in lines {
        if line.is_empty()
            || line
                .bytes()
                .any(|byte| (byte < 32 && byte != b'\t') || byte == 127)
        {
            return Err(WebhookHttpClientError::MalformedResponse);
        }
        let (name, value) = line
            .split_once(':')
            .ok_or(WebhookHttpClientError::MalformedResponse)?;
        if name.is_empty() || !name.bytes().all(is_http_token_byte) {
            return Err(WebhookHttpClientError::MalformedResponse);
        }
        if name.eq_ignore_ascii_case("retry-after") {
            if saw_retry_after {
                return Err(WebhookHttpClientError::MalformedResponse);
            }
            saw_retry_after = true;
            retry_after_ms = retry_after_milliseconds(value.trim(), now);
        }
    }
    Ok(WebhookHttpResponse {
        status,
        retry_after_ms,
    })
}

fn retry_after_milliseconds(value: &str, now: SystemTime) -> Option<u64> {
    if let Ok(seconds) = value.parse::<u64>() {
        return Some(seconds.saturating_mul(1_000));
    }
    let retry_at = httpdate::parse_http_date(value).ok()?;
    let delay = retry_at.duration_since(now).unwrap_or_default();
    Some(u64::try_from(delay.as_millis()).unwrap_or(u64::MAX))
}

#[cfg(test)]
mod webhook_http_client_tests {
    use std::time::{Duration, UNIX_EPOCH};

    use super::parse_http_response_at;

    #[test]
    fn retry_after_http_date_uses_reference_time_and_saturates_past_dates() {
        let now = UNIX_EPOCH + Duration::from_secs(1_445_412_478);
        let future = parse_http_response_at(
            "HTTP/1.1 429 Too Many Requests\r\nRetry-After: Wed, 21 Oct 2015 07:28:00 GMT\r\n\r\n",
            now,
        )
        .expect("future HTTP date parses");
        assert_eq!(future.retry_after_ms, Some(2_000));

        let past = parse_http_response_at(
            "HTTP/1.1 429 Too Many Requests\r\nRetry-After: Wed, 21 Oct 2015 07:27:00 GMT\r\n\r\n",
            now,
        )
        .expect("past HTTP date parses");
        assert_eq!(past.retry_after_ms, Some(0));
    }

    #[test]
    fn retry_after_seconds_and_invalid_dates_remain_safe() {
        let now = UNIX_EPOCH + Duration::from_secs(1_445_412_478);
        let saturated = parse_http_response_at(
            &format!(
                "HTTP/1.1 429 Too Many Requests\r\nRetry-After: {}\r\n\r\n",
                u64::MAX
            ),
            now,
        )
        .expect("integer Retry-After parses");
        assert_eq!(saturated.retry_after_ms, Some(u64::MAX));

        let invalid = parse_http_response_at(
            "HTTP/1.1 429 Too Many Requests\r\nRetry-After: not-a-date\r\n\r\n",
            now,
        )
        .expect("invalid Retry-After does not invalidate the response");
        assert_eq!(invalid.retry_after_ms, None);
    }

    #[test]
    fn malformed_status_lines_and_ambiguous_retry_headers_are_rejected() {
        let now = UNIX_EPOCH + Duration::from_secs(1_445_412_478);
        for response in [
            "NOTHTTP 200 OK\r\n\r\n",
            "HTTP/2 200 OK\r\n\r\n",
            "HTTP/1.1 99 Continue\r\n\r\n",
            "HTTP/1.1 600 Invalid\r\n\r\n",
            "HTTP/1.1 429 Rate Limited\r\nRetry-After: 1\r\nRetry-After: 2\r\n\r\n",
            "HTTP/1.1 200 OK\nX-Injected: true\r\n\r\n",
        ] {
            assert_eq!(
                parse_http_response_at(response, now),
                Err(super::WebhookHttpClientError::MalformedResponse),
                "response should be rejected: {response:?}",
            );
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerRegistryError {
    UnknownWorker {
        worker_id: String,
    },
    DrainPlan {
        source: WorkerDrainError,
    },
    IncompatibleMessageProtocolVersion {
        expected: u16,
        actual: u16,
    },
    EmptyMessageId,
    EmptyCorrelationId,
    EmptyCausationId,
    KindPayloadMismatch {
        kind: WorkerProtocolMessageKind,
        payload_kind: WorkerProtocolMessageKind,
    },
    UnexpectedWorkerMessageKind {
        kind: WorkerProtocolMessageKind,
    },
    InvalidWireMessage {
        field: &'static str,
        expected: &'static str,
    },
    WirePayloadDecode {
        kind: WorkerProtocolMessageKind,
        source: String,
    },
}

fn parse_worker_advertisement_message_wire_value(
    value: &Value,
) -> Result<WorkerProtocolMessage, WorkerRegistryError> {
    let Some(object) = value.as_object() else {
        return Err(WorkerRegistryError::InvalidWireMessage {
            field: "$",
            expected: "object",
        });
    };
    let protocol_version = optional_wire_u16(
        object,
        "protocolVersion",
        "protocol_version",
        WORKER_PROTOCOL_VERSION,
        "$.protocolVersion",
    )?;
    let message_id = required_wire_string(object, "messageId", "message_id", "$.messageId")?;
    let kind_value =
        wire_alias(object, "kind", "kind").ok_or(WorkerRegistryError::InvalidWireMessage {
            field: "$.kind",
            expected: "worker protocol message kind",
        })?;
    let kind =
        serde_json::from_value::<WorkerProtocolMessageKind>(kind_value.clone()).map_err(|_| {
            WorkerRegistryError::InvalidWireMessage {
                field: "$.kind",
                expected: "worker protocol message kind",
            }
        })?;
    if kind != WorkerProtocolMessageKind::Advertisement {
        return Err(WorkerRegistryError::UnexpectedWorkerMessageKind { kind });
    }
    let sequence = required_wire_u64(object, "sequence", "sequence", "$.sequence")?;
    let correlation_id =
        optional_wire_string(object, "correlationId", "correlation_id", "$.correlationId")?;
    let causation_id =
        optional_wire_string(object, "causationId", "causation_id", "$.causationId")?;
    let payload = wire_alias(object, "payload", "payload").ok_or(
        WorkerRegistryError::InvalidWireMessage {
            field: "$.payload",
            expected: "advertisement payload",
        },
    )?;
    let advertisement =
        serde_json::from_value::<WorkerAdvertisement>(payload.clone()).map_err(|source| {
            WorkerRegistryError::WirePayloadDecode {
                kind,
                source: source.to_string(),
            }
        })?;

    Ok(WorkerProtocolMessage {
        protocol_version,
        message_id: message_id.to_owned(),
        kind,
        sequence,
        correlation_id: correlation_id.map(str::to_owned),
        causation_id: causation_id.map(str::to_owned),
        payload: WorkerProtocolMessagePayload::Advertisement(advertisement),
    })
}

fn wire_alias<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
) -> Option<&'a Value> {
    object.get(primary).or_else(|| object.get(alternate))
}

fn required_wire_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &'static str,
    alternate: &'static str,
    field: &'static str,
) -> Result<&'a str, WorkerRegistryError> {
    wire_alias(object, primary, alternate)
        .and_then(Value::as_str)
        .ok_or(WorkerRegistryError::InvalidWireMessage {
            field,
            expected: "string",
        })
}

fn optional_wire_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &'static str,
    alternate: &'static str,
    field: &'static str,
) -> Result<Option<&'a str>, WorkerRegistryError> {
    let Some(value) = wire_alias(object, primary, alternate) else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_str()
        .map(Some)
        .ok_or(WorkerRegistryError::InvalidWireMessage {
            field,
            expected: "string or null",
        })
}

fn required_wire_u64(
    object: &serde_json::Map<String, Value>,
    primary: &'static str,
    alternate: &'static str,
    field: &'static str,
) -> Result<u64, WorkerRegistryError> {
    wire_alias(object, primary, alternate)
        .and_then(Value::as_u64)
        .ok_or(WorkerRegistryError::InvalidWireMessage {
            field,
            expected: "unsigned integer",
        })
}

fn optional_wire_u16(
    object: &serde_json::Map<String, Value>,
    primary: &'static str,
    alternate: &'static str,
    default_value: u16,
    field: &'static str,
) -> Result<u16, WorkerRegistryError> {
    let Some(value) = wire_alias(object, primary, alternate) else {
        return Ok(default_value);
    };
    let Some(value) = value.as_u64() else {
        return Err(WorkerRegistryError::InvalidWireMessage {
            field,
            expected: "unsigned integer",
        });
    };
    u16::try_from(value).map_err(|_| WorkerRegistryError::InvalidWireMessage {
        field,
        expected: "u16",
    })
}
