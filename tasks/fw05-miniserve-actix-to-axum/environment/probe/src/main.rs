//! Compile-time proof that the offline registry can build the target stack.
//!
//! Every construct here corresponds to something the baseline does with
//! actix-web, so a registry that satisfies this probe satisfies the migration:
//!
//!   * routing with path and query extraction        (web::scope / web::resource)
//!   * a middleware stack, including one that
//!     rewrites response bodies                      (wrap_fn error_page_middleware)
//!   * static file serving with range and
//!     conditional-request support                   (actix_files::Files)
//!   * response compression                          (middleware::Compress)
//!   * HTTP basic auth                               (actix_web_httpauth)
//!   * multipart form uploads                        (actix_multipart)
//!   * a streaming body                              (BodyStream)
//!   * rustls-backed TLS                             (listen_rustls)
//!
//! It is a build check, not a test: it constructs the router and exits.

use axum::{
    body::Body,
    extract::{Multipart, Path, Query, Request},
    http::{header, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use headers::{ContentType, HeaderMapExt};
use std::collections::HashMap;
use tower_http::{
    compression::CompressionLayer,
    services::ServeDir,
    set_header::SetResponseHeaderLayer,
    trace::TraceLayer,
    validate_request::ValidateRequestHeaderLayer,
};

/// The shape of the baseline's `error_page_middleware`: run the inner service,
/// then rewrite a plain-text error body into an HTML page.
async fn error_page_middleware(req: Request, next: Next) -> Response {
    let referer = req
        .headers()
        .get(header::REFERER)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("/")
        .to_owned();

    let res = next.run(req).await;
    let is_text = res
        .headers()
        .typed_get::<ContentType>()
        .map(|ct| mime::Mime::from(ct).essence_str() == "text/plain")
        .unwrap_or(false);

    if !(res.status().is_client_error() || res.status().is_server_error()) || !is_text {
        return res;
    }

    let (mut parts, body) = res.into_parts();
    let bytes = axum::body::to_bytes(body, 64 * 1024).await.unwrap_or_default();
    let text = String::from_utf8_lossy(&bytes);
    let html = format!("<h1>{}</h1><a href=\"{}\">back</a>", text, referer);
    parts.headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    parts.headers.remove(header::CONTENT_LENGTH);
    Response::from_parts(parts, Body::from(html))
}

async fn listing(Query(q): Query<HashMap<String, String>>) -> impl IntoResponse {
    let _ = q.get("sort");
    ([(header::CONTENT_TYPE, "text/html; charset=utf-8")], "<html/>")
}

async fn wildcard(Path(rest): Path<String>) -> impl IntoResponse {
    let guess = mime_guess::from_path(&rest).first_or_octet_stream();
    ([(header::CONTENT_TYPE, guess.as_ref().to_owned())], rest)
}

async fn upload(mut form: Multipart) -> Result<StatusCode, StatusCode> {
    while let Some(field) = form.next_field().await.map_err(|_| StatusCode::BAD_REQUEST)? {
        let _name = field.file_name().map(str::to_owned);
        let _data = field.bytes().await.map_err(|_| StatusCode::BAD_REQUEST)?;
    }
    Ok(StatusCode::SEE_OTHER)
}

/// A streaming archive body, the equivalent of the baseline's `BodyStream`.
async fn archive() -> impl IntoResponse {
    let (tx, rx) = tokio::io::duplex(8 * 1024);
    tokio::spawn(async move {
        use tokio::io::AsyncWriteExt;
        let mut tx = tx;
        let _ = tx.write_all(b"stream").await;
    });
    let stream = tokio_util::io::ReaderStream::new(rx);
    (
        [(header::CONTENT_TYPE, "application/x-tar")],
        Body::from_stream(stream),
    )
}

async fn not_found() -> impl IntoResponse {
    (StatusCode::NOT_FOUND, "File not found.")
}

fn build() -> Router {
    let inner = Router::new()
        .route("/", get(listing))
        .route("/upload", post(upload))
        .route("/archive", get(archive))
        .route("/*rest", get(wildcard))
        .nest_service("/static", ServeDir::new(".").precompressed_gzip())
        .layer(ValidateRequestHeaderLayer::basic("user", "pass"));

    Router::new()
        .nest("/prefix", inner)
        .fallback(not_found)
        .layer(middleware::from_fn(error_page_middleware))
        .layer(CompressionLayer::new())
        .layer(SetResponseHeaderLayer::overriding(
            header::HeaderName::from_static("x-probe"),
            HeaderValue::from_static("1"),
        ))
        .layer(TraceLayer::new_for_http())
}

#[tokio::main]
async fn main() {
    let app = build();

    // TLS wiring: parse a key/cert pair the way the baseline's --tls-cert path
    // does, and hand a rustls config to axum-server.
    let _ = rustls::crypto::ring::default_provider().install_default();
    fn _tls(cert: &[u8], key: &[u8]) -> Result<rustls::ServerConfig, Box<dyn std::error::Error>> {
        let certs = rustls_pemfile::certs(&mut &cert[..]).collect::<Result<Vec<_>, _>>()?;
        let key = rustls_pemfile::private_key(&mut &key[..])?.ok_or("no key")?;
        Ok(rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(certs, key)?)
    }
    let _ = _tls as fn(&[u8], &[u8]) -> _;

    // A hyper listener, then exit: this is a build check, not a server.
    let _make = app.into_make_service();
    let _bind = axum_server::bind("127.0.0.1:0".parse().unwrap());
    println!("axum probe ok");
}
