use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, TcpListener};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use graphblocks_runtime_core::callback_delivery::WebhookHttpRequest;
use graphblocksd::{StdWebhookHttpClient, WebhookHttpClient, WebhookHttpClientError};
use rcgen::{CertifiedKey, generate_simple_self_signed};
use rustls::pki_types::{PrivateKeyDer, PrivatePkcs8KeyDer};
use rustls::{ServerConfig, ServerConnection, StreamOwned};
use serde_json::json;

const VALIDATED_TEST_ADDRESSES: [IpAddr; 1] = [IpAddr::V4(Ipv4Addr::LOCALHOST)];

fn read_http_request(stream: &mut impl Read) -> String {
    let mut received = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let read = stream
            .read(&mut buffer)
            .expect("request should be readable");
        assert!(read > 0, "client closed before request completed");
        received.extend_from_slice(&buffer[..read]);
        let request = String::from_utf8_lossy(&received);
        let Some(header_end) = request.find("\r\n\r\n") else {
            continue;
        };
        let content_length = request[..header_end]
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().expect("content length"))
            })
            .unwrap_or(0);
        if received.len() >= header_end + 4 + content_length {
            return String::from_utf8(received).expect("request should be utf8");
        }
    }
}

#[test]
fn std_webhook_http_client_delivers_https_to_validated_address() {
    let CertifiedKey { cert, signing_key } =
        generate_simple_self_signed(vec!["callbacks.example.test".to_string()])
            .expect("test certificate generates");
    let certificate = cert.der().clone();
    let private_key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(signing_key.serialize_der()));
    let server_config = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(vec![certificate.clone()], private_key)
        .expect("TLS server config is valid");
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener binds");
    let listener_address = listener.local_addr().expect("listener has address");
    let server = thread::spawn(move || {
        let (stream, _) = listener.accept().expect("client connects");
        let connection =
            ServerConnection::new(Arc::new(server_config)).expect("TLS connection initializes");
        let mut stream = StreamOwned::new(connection, stream);
        let request = read_http_request(&mut stream);
        assert!(request.starts_with("POST /?delivery=del-1 HTTP/1.1\r\n"));
        assert!(request.contains(&format!(
            "\r\nHost: callbacks.example.test:{}\r\n",
            listener_address.port(),
        )));
        stream
            .write_all(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            .expect("response writes");
    });
    let request = WebhookHttpRequest {
        url: format!(
            "https://callbacks.example.test:{}?delivery=del-1",
            listener_address.port(),
        ),
        method: "POST".to_owned(),
        headers: BTreeMap::new(),
        body: json!({"secure": true}),
    };
    let mut client = StdWebhookHttpClient::with_root_certificates(
        Duration::from_secs(2),
        [certificate.as_ref().to_vec()],
    )
    .expect("test root is valid");

    let response = client
        .send(request, &[listener_address.ip()])
        .expect("HTTPS request succeeds over the validated address");

    assert_eq!(response.status, 204);
    server.join().expect("server joins");
}

#[test]
fn std_webhook_http_client_supports_http_query_without_path() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener binds");
    let listener_address = listener.local_addr().expect("listener has address");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("client connects");
        let request = read_http_request(&mut stream);
        assert!(request.starts_with("POST /?delivery=del-1 HTTP/1.1\r\n"));
        stream
            .write_all(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            .expect("response writes");
    });
    let request = WebhookHttpRequest {
        url: format!("http://{listener_address}?delivery=del-1"),
        method: "POST".to_owned(),
        headers: BTreeMap::new(),
        body: json!({}),
    };
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    let response = client
        .send(request, &[listener_address.ip()])
        .expect("query-only HTTP URL succeeds");

    assert_eq!(response.status, 204);
    server.join().expect("server joins");
}

#[test]
fn std_webhook_http_client_posts_canonical_json_and_parses_retry_after() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener binds");
    let listener_address = listener.local_addr().expect("listener has address");
    let url = format!("http://{}/callbacks/deliveries", listener_address);
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("client connects");
        let request = read_http_request(&mut stream);
        assert!(request.starts_with("POST /callbacks/deliveries HTTP/1.1\r\n"));
        assert!(request.contains("\r\nGraphBlocks-Delivery-Id: del-1\r\n"));
        assert!(request.contains("\r\nContent-Type: application/json\r\n"));
        assert!(request.ends_with(r#"{"a":1,"z":2}"#));
        stream
            .write_all(
                b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 2\r\nContent-Length: 0\r\n\r\n",
            )
            .expect("response writes");
    });

    let mut headers = BTreeMap::new();
    headers.insert("GraphBlocks-Delivery-Id".to_owned(), "del-1".to_owned());
    let request = WebhookHttpRequest {
        url,
        method: "POST".to_owned(),
        headers,
        body: json!({"z": 2, "a": 1}),
    };
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    let response = client
        .send(request, &[listener_address.ip()])
        .expect("request succeeds");

    assert_eq!(response.status, 429);
    assert_eq!(response.retry_after_ms, Some(2_000));
    server.join().expect("server joins");
}

#[test]
fn std_webhook_http_client_rejects_oversized_response_headers() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener binds");
    let listener_address = listener.local_addr().expect("listener has address");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("client connects");
        let _request = read_http_request(&mut stream);
        let mut response = b"HTTP/1.1 200 OK\r\nX-Oversized: ".to_vec();
        response.extend(std::iter::repeat_n(b'a', 65 * 1024));
        stream.write_all(&response).expect("response writes");
    });
    let request = WebhookHttpRequest {
        url: format!("http://{listener_address}/callbacks/deliveries"),
        method: "POST".to_owned(),
        headers: BTreeMap::new(),
        body: json!({}),
    };
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    assert_eq!(
        client.send(request, &[listener_address.ip()]),
        Err(WebhookHttpClientError::ResponseHeadersTooLarge),
    );
    server.join().expect("server joins");
}

#[test]
fn std_webhook_http_client_connects_only_to_validated_address() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener binds");
    let address = listener.local_addr().expect("listener has address");
    let url = format!(
        "http://does-not-resolve.invalid:{}/callbacks/deliveries",
        address.port()
    );
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("client connects");
        let request = read_http_request(&mut stream);
        assert!(request.contains(&format!(
            "\r\nHost: does-not-resolve.invalid:{}\r\n",
            address.port()
        )));
        stream
            .write_all(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            .expect("response writes");
    });
    let request = WebhookHttpRequest {
        url,
        method: "POST".to_owned(),
        headers: BTreeMap::new(),
        body: json!({}),
    };
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    let response = client
        .send(request, &[address.ip()])
        .expect("request uses validated address without resolving the URL host");

    assert_eq!(response.status, 204);
    server.join().expect("server joins");
}

#[test]
fn std_webhook_http_client_rejects_unsupported_scheme_before_network_io() {
    let request = WebhookHttpRequest {
        url: "ftp://callbacks.example.test/events".to_owned(),
        method: "POST".to_owned(),
        headers: BTreeMap::new(),
        body: json!({}),
    };
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    assert_eq!(
        client.send(request, &VALIDATED_TEST_ADDRESSES),
        Err(WebhookHttpClientError::UnsupportedScheme("ftp".to_owned())),
    );
}

#[test]
fn std_webhook_http_client_rejects_header_injection_before_network_io() {
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));
    for (name, value) in [
        ("GraphBlocks-Delivery-Id", "del-1\r\nX-Injected: true"),
        ("Bad Header", "value"),
        ("GraphBlocks-Delivery-Id", "del-1\0trailer"),
    ] {
        let mut headers = BTreeMap::new();
        headers.insert(name.to_owned(), value.to_owned());
        let request = WebhookHttpRequest {
            url: "http://127.0.0.1:9/callbacks".to_owned(),
            method: "POST".to_owned(),
            headers,
            body: json!({}),
        };

        assert_eq!(
            client.send(request, &VALIDATED_TEST_ADDRESSES),
            Err(WebhookHttpClientError::InvalidHeader),
            "header {name:?}: {value:?} should be rejected before connect",
        );
    }
}

#[test]
fn std_webhook_http_client_rejects_malformed_authority_before_network_io() {
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    for url in [
        "http://hooks example.com/callbacks",
        "http://hooks.example.com\t/callbacks",
        "http://hooks.example.com%2fevil.test/callbacks",
        "http://[not-ipv6]/callbacks",
        "http://[fe80::1%25eth0]/callbacks",
    ] {
        let request = WebhookHttpRequest {
            url: url.to_owned(),
            method: "POST".to_owned(),
            headers: BTreeMap::new(),
            body: json!({}),
        };

        assert_eq!(
            client.send(request, &VALIDATED_TEST_ADDRESSES),
            Err(WebhookHttpClientError::MalformedUrl),
            "{url} should be rejected before connect"
        );
    }
}

#[test]
fn std_webhook_http_client_rejects_malformed_request_target_before_network_io() {
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    for url in [
        "http://127.0.0.1:9/callback\r\nX-Injected: true",
        "http://127.0.0.1:9/callback\ttrail",
        "http://127.0.0.1:9/callback with space",
        "http://127.0.0.1:9/callback#fragment",
        "https://callbacks.example.test?delivery=del-1#fragment",
    ] {
        let request = WebhookHttpRequest {
            url: url.to_owned(),
            method: "POST".to_owned(),
            headers: BTreeMap::new(),
            body: json!({}),
        };

        assert_eq!(
            client.send(request, &VALIDATED_TEST_ADDRESSES),
            Err(WebhookHttpClientError::MalformedUrl),
            "{url:?} should be rejected before connect"
        );
    }
}
