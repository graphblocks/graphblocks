use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

use graphblocks_runtime_core::callback_delivery::WebhookHttpRequest;
use graphblocksd::{StdWebhookHttpClient, WebhookHttpClient, WebhookHttpClientError};
use serde_json::json;

fn read_http_request(stream: &mut std::net::TcpStream) -> String {
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
fn std_webhook_http_client_posts_canonical_json_and_parses_retry_after() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener binds");
    let url = format!(
        "http://{}/callbacks/deliveries",
        listener.local_addr().expect("listener has address")
    );
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

    let response = client.send(request).expect("request succeeds");

    assert_eq!(response.status, 429);
    assert_eq!(response.retry_after_ms, Some(2_000));
    server.join().expect("server joins");
}

#[test]
fn std_webhook_http_client_rejects_unsupported_scheme_before_network_io() {
    let request = WebhookHttpRequest {
        url: "https://callbacks.example.test/events".to_owned(),
        method: "POST".to_owned(),
        headers: BTreeMap::new(),
        body: json!({}),
    };
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    assert_eq!(
        client.send(request),
        Err(WebhookHttpClientError::UnsupportedScheme(
            "https".to_owned()
        )),
    );
}

#[test]
fn std_webhook_http_client_rejects_header_injection_before_network_io() {
    let mut headers = BTreeMap::new();
    headers.insert(
        "GraphBlocks-Delivery-Id".to_owned(),
        "del-1\r\nX-Injected: true".to_owned(),
    );
    let request = WebhookHttpRequest {
        url: "http://127.0.0.1:9/callbacks".to_owned(),
        method: "POST".to_owned(),
        headers,
        body: json!({}),
    };
    let mut client = StdWebhookHttpClient::new(Duration::from_secs(2));

    assert_eq!(
        client.send(request),
        Err(WebhookHttpClientError::InvalidHeader)
    );
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
            client.send(request),
            Err(WebhookHttpClientError::MalformedUrl),
            "{url} should be rejected before connect"
        );
    }
}
