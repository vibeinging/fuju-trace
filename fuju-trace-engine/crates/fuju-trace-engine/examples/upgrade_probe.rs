//! 用进程内 API 检查旧数据目录升级、查询与重复写入，不启动网络服务。
use fuju_trace_engine::{EngineJsonApi, WriteCoordinator};

fn request(api: &EngineJsonApi, method: &str, path: &str, body: &str) -> String {
    let (status, response) = api.route_with_tenant(method, path, body, Some(42));
    assert_eq!(status, 200, "{method} {path}: {response}");
    response
}

fn main() {
    let mut args = std::env::args().skip(1);
    let action = args.next().expect("expected first or second");
    let dir = args.next().expect("expected data directory");
    let coord = WriteCoordinator::open_durable(dir).expect("open old data");
    coord.recover();
    let api = EngineJsonApi::new(coord);
    let traces_before = request(&api, "GET", "/v1/traces", "");
    assert!(traces_before.contains("900001"), "custom trace missing");
    let search = request(&api, "POST", "/v1/search", r#"{"text":"盗刷","k":20}"#);
    assert!(search.contains("900001"), "custom event not searchable");
    let filtered = request(
        &api,
        "POST",
        "/v1/search",
        r#"{"text":"盗刷","k":20,"filter":{"attrs":{"project_id":"scale-a"}}}"#,
    );
    assert_ne!(filtered, "[]", "attribute-filtered search lost old data");
    let rollup = request(
        &api,
        "POST",
        "/v1/trace-aggregate",
        r#"{"filter":{"projectId":"scale-a"},"groupBy":["skill"],"limit":20}"#,
    );
    assert!(rollup.contains("items"), "aggregate missing items");
    let detail_before = request(&api, "GET", "/v1/traces/900001", "");
    let custom_before = request(
        &api,
        "POST",
        "/v1/search",
        r#"{"text":"升级去重验证","k":10}"#,
    );
    if action == "first" {
        let event = r#"[{"trace_id":900001,"span_id":900001,"session_id":900001,"ts":900001,"seq":1,"event_type":3,"ext_span_id":"upgrade-span","status":0,"agent_name":"upgrade-agent","attrs":{"project_id":"upgrade-project","skill":"upgrade"},"logs":["升级去重验证 盗刷"]}]"#;
        request(&api, "POST", "/v1/ingest", event);
        let detail_after = request(&api, "GET", "/v1/traces/900001", "");
        let custom_after = request(
            &api,
            "POST",
            "/v1/search",
            r#"{"text":"升级去重验证","k":10}"#,
        );
        assert_eq!(detail_before, detail_after, "duplicate retry changed trace");
        assert_eq!(
            custom_before, custom_after,
            "duplicate retry changed search"
        );
        assert_eq!(traces_before, request(&api, "GET", "/v1/traces", ""));
    } else {
        assert_eq!(action, "second", "expected first or second");
    }
    println!("upgrade probe {action} passed");
}
