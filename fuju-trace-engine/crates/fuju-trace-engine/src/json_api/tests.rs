use super::*;
use crate::InMemorySegmentStore;

fn api() -> EngineJsonApi {
    EngineJsonApi::new(WriteCoordinator::new(Arc::new(
        InMemorySegmentStore::default(),
    )))
}

const BATCH: &str = r#"[
  {"trace_id":7,"span_id":1,"ts":100,"seq":1,"event_type":1,"ext_span_id":"7-1","status":0,"input_tokens":900,"logs":["开始"]},
  {"trace_id":7,"span_id":1,"ts":150,"seq":2,"event_type":2,"ext_span_id":"7-1","duration_ns":50,"output_tokens":150,"logs":["结束"]}
]"#;

#[test]
fn route_ingest_then_query() {
    let s = api();
    let (status, body) = s.route("POST", "/v1/ingest", BATCH);
    assert_eq!(status, 200);
    assert!(body.contains("\"ingested\":2"));

    let (status, body) = s.route("GET", "/v1/traces", "");
    assert_eq!(status, 200);
    assert!(body.contains("\"trace_id\":7"), "{body}");
    assert!(body.contains("\"total_input_tokens\":900"));
}

fn trace_search_page_events() -> String {
    let events = (0..30)
        .map(|i| {
            format!(
                r#"{{"trace_id":{},"span_id":{},"ts":{},"seq":1,"event_type":2,"ext_span_id":"page-{i}","duration_ns":{},"input_tokens":{},"status":{}}}"#,
                i / 3 + 1,
                i % 3 + 1,
                i,
                (i * 7) % 11,
                (i * 5) % 9,
                i % 2,
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    format!("[{events}]")
}

fn assert_trace_search_pages(s: &EngineJsonApi) {
    for sort in ["trace", "duration", "tokens", "status"] {
        let body = format!(r#"{{"sortBy":"{sort}","limit":500}}"#);
        let (status, full) = s.route("POST", "/v1/trace-search", &body);
        assert_eq!(status, 200, "{full}");
        let full = crate::wire::parse(&full).unwrap();
        let crate::wire::Json::Arr(all) = full.get("items").unwrap() else {
            panic!("expected items array");
        };
        assert_eq!(all.len(), 30);
        for cursor in [0usize, 1, 5, 19, 29, 30, 40] {
            let body = format!(r#"{{"sortBy":"{sort}","cursor":{cursor},"limit":4}}"#);
            let (status, page) = s.route("POST", "/v1/trace-search", &body);
            assert_eq!(status, 200, "{page}");
            let page = crate::wire::parse(&page).unwrap();
            assert_eq!(page.get("total").unwrap().as_u64(), Some(30));
            let crate::wire::Json::Arr(items) = page.get("items").unwrap() else {
                panic!("expected page items array");
            };
            assert_eq!(
                items,
                &all[cursor.min(30)..cursor.saturating_add(4).min(30)]
            );
        }
    }
}

#[test]
fn trace_search_partial_pages_match_full_sort_for_every_order() {
    let s = api();
    let (status, response) = s.route("POST", "/v1/ingest", &trace_search_page_events());
    assert_eq!(status, 200, "{response}");
    assert_trace_search_pages(&s);
}

#[test]
fn trace_search_pages_match_full_sort_after_disk_reopen() {
    let dir = std::env::temp_dir().join(format!(
        "fuju_trace_page_{}_{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
    ));
    let coord = WriteCoordinator::open_durable(&dir).unwrap();
    let s = EngineJsonApi::new(Arc::clone(&coord));
    let (status, response) = s.route("POST", "/v1/ingest", &trace_search_page_events());
    assert_eq!(status, 200, "{response}");
    coord.flush_memtable();
    drop(s);
    drop(coord);

    let reopened = WriteCoordinator::open_durable(&dir).unwrap();
    reopened.recover();
    let s = EngineJsonApi::new(reopened);
    assert_trace_search_pages(&s);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn structured_cache_usage_round_trips_and_aggregates_without_double_counting() {
    let s = api();
    let events = r#"[
      {"trace_id":702,"span_id":1,"session_id":9002,"ts":1,"seq":1,"event_type":1,"ext_span_id":"702-1","span_name":"llm:chat","model":"qwen"},
      {"trace_id":702,"span_id":1,"session_id":9002,"ts":2,"seq":2,"event_type":2,"ext_span_id":"702-1","input_tokens":100,"output_tokens":20,"cacheReadTokens":0,"cacheWriteTokens":30,"attrs":{"llm.call_site":"superagent.reasoning"}}
    ]"#;
    let (status, body) = s.route("POST", "/v1/ingest", events);
    assert_eq!(status, 200, "{body}");

    let (status, trace) = s.route("GET", "/v1/traces/702", "");
    assert_eq!(status, 200, "{trace}");
    assert!(trace.contains(r#""cacheReadTokens":0"#), "{trace}");
    assert!(trace.contains(r#""cacheWriteTokens":30"#), "{trace}");
    assert!(trace.contains(r#""totalCacheReadTokens":0"#), "{trace}");
    assert!(trace.contains(r#""cacheReadReportedSpans":1"#), "{trace}");
    assert!(trace.contains(r#""totalLlmSpans":1"#), "{trace}");

    let (status, aggregate) = s.route("POST", "/v1/trace-aggregate", r#"{"groupBy":["model"]}"#);
    assert_eq!(status, 200, "{aggregate}");
    assert!(aggregate.contains(r#""cacheReadTokens":0"#), "{aggregate}");
    assert!(
        aggregate.contains(r#""cacheWriteTokens":30"#),
        "{aggregate}"
    );
    assert!(aggregate.contains(r#""spanCount":1"#), "{aggregate}");

    let (status, trajectory) = s.route("POST", "/v1/trace-trajectories", r#"{"limit":10}"#);
    assert_eq!(status, 200, "{trajectory}");
    assert!(
        trajectory.contains(r#""cacheReadTokens":0"#),
        "{trajectory}"
    );
    assert!(
        trajectory.contains(r#""cacheWriteTokens":30"#),
        "{trajectory}"
    );
}

#[test]
fn start_only_span_is_running_until_end_arrives() {
    let s = api();
    let start = r#"[{
      "trace_id":701,"span_id":1,"session_id":9001,"ts":100,"seq":1,
      "event_type":1,"ext_span_id":"701-1","span_name":"长时任务"
    }]"#;
    let (status, body) = s.route("POST", "/v1/ingest", start);
    assert_eq!(status, 200, "{body}");

    let (status, trace) = s.route("GET", "/v1/traces/701", "");
    assert_eq!(status, 200, "{trace}");
    assert!(trace.contains(r#""status":"run""#), "{trace}");
    assert!(trace.contains(r#""lifecycle":"running""#), "{trace}");
    assert!(!trace.contains(r#""status":"ok""#), "{trace}");

    let (status, sessions) = s.route("GET", "/v1/sessions?limit=10", "");
    assert_eq!(status, 200, "{sessions}");
    assert!(sessions.contains(r#""status":"run""#), "{sessions}");

    // end 不带 duration/status 也必须依据事件类型变成 completed。
    let end = r#"[{
      "trace_id":701,"span_id":1,"session_id":9001,"ts":200,"seq":2,
      "event_type":2,"ext_span_id":"701-1"
    }]"#;
    let (status, body) = s.route("POST", "/v1/ingest", end);
    assert_eq!(status, 200, "{body}");

    let (status, trace) = s.route("GET", "/v1/traces/701", "");
    assert_eq!(status, 200, "{trace}");
    assert!(trace.contains(r#""status":"ok""#), "{trace}");
    assert!(trace.contains(r#""lifecycle":"completed""#), "{trace}");
}

#[test]
fn names_are_exposed_without_changing_search_or_actor_identity() {
    let s = api();
    let batch = r#"[
      {"trace_id":501,"span_id":1,"ts":1,"seq":1,"event_type":1,"ext_span_id":"501-1","span_name":"内部检索名甲","display_name":"  用户看到的根节点乙  ","agent_name":"risk-agent"},
      {"trace_id":501,"span_id":2,"parent_span_id":1,"ts":2,"seq":1,"event_type":1,"ext_span_id":"501-2","span_name":"内部工具检索名丙","display_name":"用户看到的工具丁","agent_name":"risk-agent","tool_name":"customer_lookup"},
      {"trace_id":501,"span_id":1,"ts":3,"seq":2,"event_type":2,"ext_span_id":"501-1","duration_ns":10},
      {"trace_id":501,"span_id":2,"parent_span_id":1,"ts":4,"seq":2,"event_type":2,"ext_span_id":"501-2","duration_ns":5}
    ]"#;
    let (status, body) = s.route("POST", "/v1/ingest", batch);
    assert_eq!(status, 200, "{body}");

    let (status, trace) = s.route("GET", "/v1/traces/501", "");
    assert_eq!(status, 200, "{trace}");
    assert!(trace.contains(r#""name":"用户看到的根节点乙""#), "{trace}");
    assert!(trace.contains(r#""spanName":"内部检索名甲""#), "{trace}");
    assert!(
        trace.contains(r#""displayName":"用户看到的根节点乙""#),
        "{trace}"
    );
    assert!(trace.contains(r#""actorId":"agent:risk-agent""#), "{trace}");
    assert!(
        trace.contains(r#""actorId":"tool:customer_lookup""#),
        "{trace}"
    );
    assert!(
        trace.contains(r#""kind":"tool""#),
        "继承 agent 的工具 span 仍应是工具: {trace}"
    );

    let (status, steps) = s.route("GET", "/v1/traces/501/steps", "");
    assert_eq!(status, 200, "{steps}");
    assert!(
        steps.contains(r#""displayName":"用户看到的工具丁""#),
        "{steps}"
    );

    let (status, detail) = s.route("GET", "/v1/traces/501/spans/2", "");
    assert_eq!(status, 200, "{detail}");
    assert!(
        detail.contains(r#""spanName":"内部工具检索名丙""#),
        "{detail}"
    );
    assert!(detail.contains(r#""agentName":"risk-agent""#), "{detail}");
    assert!(
        detail.contains(r#""toolName":"customer_lookup""#),
        "{detail}"
    );

    let (status, internal_search) =
        s.route("POST", "/v1/search", r#"{"text":"内部检索名甲","k":10}"#);
    assert_eq!(status, 200, "{internal_search}");
    assert!(
        internal_search.contains(r#""trace_id":501"#),
        "span_name 应可检索: {internal_search}"
    );

    let (status, display_search) = s.route(
        "POST",
        "/v1/search",
        r#"{"text":"用户看到的根节点乙","k":10}"#,
    );
    assert_eq!(status, 200, "{display_search}");
    assert_eq!(display_search, "[]", "display_name 只展示，不进入检索");

    let (status, read_model) = s.route(
        "POST",
        "/v1/trace-search",
        r#"{"filter":{"traceId":501},"limit":10}"#,
    );
    assert_eq!(status, 200, "{read_model}");
    assert!(
        read_model.contains(r#""spanName":"内部检索名甲""#),
        "{read_model}"
    );
    assert!(
        read_model.contains(r#""displayName":"用户看到的根节点乙""#),
        "{read_model}"
    );
}

#[test]
fn route_ingest_accepts_external_ids_and_attrs() {
    let s = api();
    let batch = r#"[
    {
      "trace_id":"run-uuid",
      "span_id":"span-uuid",
      "session_id":"session-uuid",
      "ts":100,
      "seq":1,
      "event_type":2,
      "ext_span_id":"span-uuid",
      "status":0,
      "duration_ns":50,
      "agent_name":"risk",
      "input_text":"疑似盗刷",
      "attrs":{"external_run_id":"run-uuid","project_id":"agentic-data","skill":"review","mode":"auto","call_site":"worker.ts:10"}
    },
    {
      "trace_id":"123456",
      "span_id":"numeric-business-span",
      "ts":120,
      "seq":1,
      "event_type":2,
      "ext_span_id":"numeric-business-span",
      "status":0,
      "input_text":"数字业务主键",
      "attrs":{"project_id":"agentic-data","skill":"review"}
    }]"#;
    let (status, body) = s.route("POST", "/v1/ingest", batch);
    assert_eq!(status, 200, "{body}");

    let (status, body) = s.route("GET", "/v1/traces/run-uuid", "");
    assert_eq!(status, 200, "{body}");
    assert!(body.contains(r#""externalTraceId":"run-uuid""#), "{body}");
    assert!(body.contains(r#""externalSpanId":"span-uuid""#), "{body}");
    assert!(body.contains(r#""project_id":"agentic-data""#), "{body}");

    let (status, body) = s.route("GET", "/v1/traces/run-uuid/spans/span-uuid", "");
    assert_eq!(status, 200, "{body}");
    assert!(body.contains(r#""externalSpanId":"span-uuid""#), "{body}");
    assert!(body.contains(r#""call_site":"worker.ts:10""#), "{body}");

    let (status, body) = s.route(
        "POST",
        "/v1/search",
        r#"{"text":"盗刷","filter":{"trace_id":"run-uuid"}}"#,
    );
    assert_eq!(status, 200, "{body}");
    assert!(body.contains(r#""external_trace_id":"run-uuid""#), "{body}");
    assert!(body.contains(r#""skill":"review""#), "{body}");

    let (status, body) = s.route(
        "POST",
        "/v1/search",
        r#"{"text":"盗刷","filter":{"attrs":{"project_id":"agentic-data","skill":"review"}}}"#,
    );
    assert_eq!(status, 200, "{body}");
    assert!(body.contains(r#""external_trace_id":"run-uuid""#), "{body}");

    let (status, body) = s.route(
        "POST",
        "/v1/trace-search",
        r#"{"filter":{"externalTraceId":"run-uuid"},"limit":1}"#,
    );
    assert_eq!(status, 200, "{body}");
    assert!(body.contains(r#""total":1"#), "{body}");
    assert!(body.contains(r#""externalTraceId":"run-uuid""#), "{body}");
    assert!(body.contains(r#""usedFilterIndex":true"#), "{body}");
    assert!(body.contains(r#""candidateSpanKeys":1"#), "{body}");

    let (status, body) = s.route(
        "POST",
        "/v1/trace-search",
        r#"{"filter":{"traceId":"123456"},"limit":1}"#,
    );
    assert_eq!(status, 200, "{body}");
    assert!(body.contains(r#""total":1"#), "{body}");
    assert!(body.contains(r#""externalTraceId":"123456""#), "{body}");
    assert!(body.contains(r#""usedFilterIndex":true"#), "{body}");
    assert!(body.contains(r#""candidateSpanKeys":1"#), "{body}");

    let (status, body) = s.route(
        "POST",
        "/v1/trace-search",
        r#"{"filter":{"traceId":123456},"limit":1}"#,
    );
    assert_eq!(status, 200, "{body}");
    assert!(body.contains(r#""total":1"#), "{body}");
    assert!(body.contains(r#""externalTraceId":"123456""#), "{body}");
    assert!(body.contains(r#""usedFilterIndex":true"#), "{body}");
    assert!(body.contains(r#""candidateSpanKeys":1"#), "{body}");

    let (status, body) = s.route(
        "POST",
        "/v1/trace-search",
        r#"{"filter":{"externalTraceId":"run-missing"},"limit":1}"#,
    );
    assert_eq!(status, 200, "{body}");
    assert!(body.contains(r#""total":0"#), "{body}");
    assert!(body.contains(r#""usedFilterIndex":true"#), "{body}");
    assert!(body.contains(r#""candidateSpanKeys":0"#), "{body}");

    let (status, body) = s.route(
        "POST",
        "/v1/search",
        r#"{"text":"盗刷","filter":{"project_id":"agentic-data","skill":"other"}}"#,
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body, "[]");
}

#[test]
fn tenant_context_isolates_traces_and_search() {
    // 进程内租户隔离：调用上下文的 tenant 覆盖 body tenant_id；
    // 列表和检索只返回调用上下文指定的租户。
    let s = api();
    let batch1 = r#"[
      {"trace_id":1,"span_id":1,"ts":100,"seq":1,"event_type":2,"ext_span_id":"1-1","tenant_id":999,"duration_ns":10,"logs":["盗刷"]}
    ]"#;
    let batch2 = r#"[
      {"trace_id":2,"span_id":1,"ts":100,"seq":1,"event_type":2,"ext_span_id":"2-1","tenant_id":999,"duration_ns":20,"logs":["盗刷"]}
    ]"#;
    assert_eq!(
        s.route_with_tenant("POST", "/v1/ingest", batch1, Some(1)).0,
        200
    );
    assert_eq!(
        s.route_with_tenant("POST", "/v1/ingest", batch2, Some(2)).0,
        200
    );

    // 不带租户：两条都列。
    let all = s.route("GET", "/v1/traces", "").1;
    assert!(all.contains("\"trace_id\":1") && all.contains("\"trace_id\":2"));
    // 带租户 1：只见 trace 1。
    let t1 = s.route_with_tenant("GET", "/v1/traces", "", Some(1)).1;
    assert!(
        t1.contains("\"trace_id\":1") && !t1.contains("\"trace_id\":2"),
        "列表按租户上下文隔离: {t1}"
    );
    // 检索同样隔离：查"盗刷"租户 1 只回 trace 1。
    let r1 = s
        .route_with_tenant("POST", "/v1/search", r#"{"text":"盗刷","k":10}"#, Some(1))
        .1;
    assert!(
        r1.contains("\"trace_id\":1") && !r1.contains("\"trace_id\":2"),
        "检索按租户上下文隔离: {r1}"
    );
    let spoofed = s.route_with_tenant("GET", "/v1/traces", "", Some(999)).1;
    assert!(
        !spoofed.contains("\"trace_id\":"),
        "body tenant_id 不应生效: {spoofed}"
    );
}

// 两条带 agent 的中文 span，用于验证检索和过滤。
const SEARCH_BATCH: &str = r#"[
  {"trace_id":1,"span_id":10,"ts":1,"seq":1,"event_type":2,"ext_span_id":"1-10","status":1,"duration_ns":100,"agent_name":"风控","logs":["疑似盗刷 已拦截"]},
  {"trace_id":2,"span_id":20,"ts":1,"seq":1,"event_type":2,"ext_span_id":"2-20","status":0,"duration_ns":50,"agent_name":"人工","logs":["盗刷误报 复核通过"]}
]"#;

#[test]
fn route_search_text_and_filter() {
    // 检索端点:灌数据 → POST /v1/search 中文搜 → 带 agent 过滤再搜。
    let s = api();
    assert_eq!(s.route("POST", "/v1/ingest", SEARCH_BATCH).0, 200);

    // 纯文本搜"盗刷":两条都命中。
    let (st, body) = s.route("POST", "/v1/search", r#"{"text":"盗刷","k":10}"#);
    assert_eq!(st, 200, "{body}");
    assert!(
        body.contains("\"trace_id\":1") && body.contains("\"trace_id\":2"),
        "{body}"
    );

    // 加 agent 过滤:只剩风控那条。
    let (st2, body2) = s.route(
        "POST",
        "/v1/search",
        r#"{"text":"盗刷","k":10,"filter":{"agent_name":"风控"}}"#,
    );
    assert_eq!(st2, 200);
    assert!(body2.contains("\"trace_id\":1"), "{body2}");
    assert!(
        !body2.contains("\"trace_id\":2"),
        "agent 过滤掉人工那条: {body2}"
    );
    assert!(body2.contains("风控"), "响应带 agent 名");

    // 坏 body → 400。
    assert_eq!(s.route("POST", "/v1/search", "not json").0, 400);
}

#[test]
fn route_search_vector_and_hybrid() {
    // 检索端点的向量 / 混合路:body 带 vector 走找相似,text+vector 走混合。
    let coord = WriteCoordinator::new(Arc::new(InMemorySegmentStore::default()));
    let s = EngineJsonApi::new(Arc::clone(&coord));
    assert_eq!(s.route("POST", "/v1/ingest", SEARCH_BATCH).0, 200);
    coord.index_embedding(1, 10, vec![0.0, 0.0]); // 风控/盗刷,离 query 近
    coord.index_embedding(2, 20, vec![5.0, 5.0]); // 人工,远

    // 只给 vector → 找相似,最近的是 span(1,10)。
    let (st, body) = s.route("POST", "/v1/search", r#"{"vector":[0.1,0.1],"k":5}"#);
    assert_eq!(st, 200, "{body}");
    assert!(
        body.contains("\"trace_id\":1"),
        "向量找相似命中近邻: {body}"
    );

    // text + vector → 混合(RRF):盗刷两条都关键词命中,(1,10) 又被向量命中 → 排更前。
    let (st2, body2) = s.route(
        "POST",
        "/v1/search",
        r#"{"text":"盗刷","vector":[0.1,0.1],"k":5}"#,
    );
    assert_eq!(st2, 200);
    assert!(
        body2.starts_with("[{\"trace_id\":1"),
        "混合里双命中的 (1,10) 居首: {body2}"
    );

    // 向量 + agent 过滤:只剩风控那条。
    let (st3, body3) = s.route(
        "POST",
        "/v1/search",
        r#"{"vector":[0.1,0.1],"k":5,"filter":{"agent_name":"风控"}}"#,
    );
    assert_eq!(st3, 200);
    assert!(
        body3.contains("\"trace_id\":1") && !body3.contains("\"trace_id\":2"),
        "{body3}"
    );
}

#[test]
fn route_console_sessions_turns_trace_detail() {
    // 控制台数据端点端到端：灌 1 个会话(2 轮) → 会话分页 → 轮次 → trace span → span 详情。
    let s = api();
    let batch = r#"[
      {"trace_id":11,"span_id":1,"ts":1,"seq":1,"event_type":1,"ext_span_id":"11-1","session_id":900,"agent_name":"风控研判","input_tokens":500,"input_text":"对账户A做研判","attrs":{"project_id":"agentic-data","skill":"review","mode":"auto"}},
      {"trace_id":11,"span_id":1,"ts":2,"seq":2,"event_type":4,"ext_span_id":"11-1","session_id":900,"logs":["读取 package.json"],"attrs":{"call_site":"package-json"}},
      {"trace_id":11,"span_id":1,"ts":3,"seq":3,"event_type":2,"ext_span_id":"11-1","session_id":900,"status":0,"duration_ns":2000000,"output_tokens":120,"output_text":"触发规则R12"},
      {"trace_id":12,"span_id":1,"ts":3,"seq":1,"event_type":1,"ext_span_id":"12-1","session_id":900,"agent_name":"风控研判","input_tokens":300,"input_text":"继续核查"},
      {"trace_id":12,"span_id":1,"ts":4,"seq":2,"event_type":2,"ext_span_id":"12-1","session_id":900,"status":0,"duration_ns":1000000,"output_tokens":80}
    ]"#;
    assert_eq!(s.route("POST", "/v1/ingest", batch).0, 200);

    // 会话分页：1 个会话、2 轮、标题取 agent。
    let (st, body) = s.route("GET", "/v1/sessions?cursor=0&limit=50", "");
    assert_eq!(st, 200, "{body}");
    assert!(body.contains("\"sessionId\":\"900\""), "{body}");
    assert!(body.contains("\"turnCount\":2"), "{body}");
    assert!(body.contains("\"title\":\"风控研判\""), "{body}");
    assert!(body.contains("\"total\":1"), "{body}");
    assert!(body.contains("\"nextCursor\":null"), "{body}");

    let (st_attr, body_attr) = s.route(
        "GET",
        "/v1/sessions?attrs=%7B%22project_id%22%3A%22agentic-data%22%2C%22skill%22%3A%22review%22%7D",
        "",
    );
    assert_eq!(st_attr, 200, "{body_attr}");
    assert!(body_attr.contains("\"sessionId\":\"900\""), "{body_attr}");
    assert!(body_attr.contains("\"total\":1"), "{body_attr}");

    let (st_miss, body_miss) = s.route(
        "GET",
        "/v1/sessions?project_id=agentic-data&skill=other",
        "",
    );
    assert_eq!(st_miss, 200, "{body_miss}");
    assert!(body_miss.contains("\"items\":[]"), "{body_miss}");
    assert!(body_miss.contains("\"total\":0"), "{body_miss}");

    // 轮次：2 轮，首轮名取 input_text。
    let (st2, turns) = s.route("GET", "/v1/sessions/900/turns", "");
    assert_eq!(st2, 200, "{turns}");
    assert!(
        turns.contains("\"turnIndex\":0") && turns.contains("\"turnIndex\":1"),
        "{turns}"
    );
    assert!(turns.contains("对账户A做研判"), "{turns}");
    assert!(turns.contains("\"durMs\":2"), "首轮 2ms: {turns}");

    // trace span：trace 11 有 span，kind=agent。
    let (st3, trace) = s.route("GET", "/v1/traces/11", "");
    assert_eq!(st3, 200, "{trace}");
    assert!(
        trace.contains("\"kind\":\"agent\"") && trace.contains("风控研判"),
        "{trace}"
    );
    assert!(trace.contains("\"summary\""), "{trace}");
    assert!(
        trace.contains("\"logEvents\"")
            && trace.contains("读取 package.json")
            && trace.contains("\"eventType\":4")
            && trace.contains("\"call_site\":\"package-json\""),
        "{trace}"
    );

    // span 详情：晚物化大字段。
    let (st4, detail) = s.route("GET", "/v1/traces/11/spans/1", "");
    assert_eq!(st4, 200, "{detail}");
    assert!(detail.contains("触发规则R12"), "{detail}");
    assert!(
        detail.contains("\"logEvents\"") && detail.contains("读取 package.json"),
        "{detail}"
    );

    // 步骤流：带输入/输出文本一次给全。
    let (st5, steps) = s.route("GET", "/v1/traces/11/steps", "");
    assert_eq!(st5, 200, "{steps}");
    assert!(
        steps.contains("对账户A做研判") && steps.contains("触发规则R12"),
        "{steps}"
    );

    // 不存在的 trace → 404。
    assert_eq!(s.route("GET", "/v1/traces/999", "").0, 404);
}

#[test]
fn route_console_endpoints_are_tenant_isolated() {
    // 控制台详情端点也必须按 X-Tenant-Id 隔离，尤其是 input/output 大文本。
    let s = api();
    let t1 = r#"[
      {"trace_id":11,"span_id":1,"ts":1,"seq":1,"event_type":1,"ext_span_id":"11-1","session_id":900,"tenant_id":999,"agent_name":"租户一","input_text":"租户一问题"},
      {"trace_id":11,"span_id":1,"ts":2,"seq":2,"event_type":2,"ext_span_id":"11-1","session_id":900,"tenant_id":999,"status":0,"duration_ns":1000000,"output_text":"租户一答案"}
    ]"#;
    let t2 = r#"[
      {"trace_id":22,"span_id":1,"ts":1,"seq":1,"event_type":1,"ext_span_id":"22-1","session_id":900,"tenant_id":999,"agent_name":"租户二","input_text":"租户二机密"},
      {"trace_id":22,"span_id":1,"ts":2,"seq":2,"event_type":2,"ext_span_id":"22-1","session_id":900,"tenant_id":999,"status":0,"duration_ns":2000000,"output_text":"租户二答案"}
    ]"#;
    assert_eq!(
        s.route_with_tenant("POST", "/v1/ingest", t1, Some(1)).0,
        200
    );
    assert_eq!(
        s.route_with_tenant("POST", "/v1/ingest", t2, Some(2)).0,
        200
    );

    let sessions1 = s
        .route_with_tenant("GET", "/v1/sessions?cursor=0&limit=50", "", Some(1))
        .1;
    assert!(sessions1.contains("\"firstTraceId\":\"11\""), "{sessions1}");
    assert!(
        !sessions1.contains("\"firstTraceId\":\"22\""),
        "{sessions1}"
    );

    let turns1 = s
        .route_with_tenant("GET", "/v1/sessions/900/turns", "", Some(1))
        .1;
    assert!(
        turns1.contains("\"traceId\":\"11\"") && turns1.contains("租户一问题"),
        "{turns1}"
    );
    assert!(!turns1.contains("租户二机密"), "{turns1}");

    let (st_cross, body_cross) = s.route_with_tenant("GET", "/v1/traces/22", "", Some(1));
    assert_eq!(st_cross, 404, "tenant1 不能读 tenant2 trace: {body_cross}");
    assert_eq!(
        s.route_with_tenant("GET", "/v1/traces/22/spans/1", "", Some(1))
            .0,
        404
    );
    assert_eq!(
        s.route_with_tenant("GET", "/v1/traces/22/steps", "", Some(1))
            .0,
        404
    );

    let (st2, trace2) = s.route_with_tenant("GET", "/v1/traces/22", "", Some(2));
    assert_eq!(st2, 200, "{trace2}");
    let detail2 = s
        .route_with_tenant("GET", "/v1/traces/22/spans/1", "", Some(2))
        .1;
    assert!(
        detail2.contains("租户二答案") && !detail2.contains("租户一答案"),
        "{detail2}"
    );
}

#[test]
fn route_rejects_bad_json_and_unknown() {
    let s = api();
    assert_eq!(s.route("POST", "/v1/ingest", "garbage").0, 400);
    assert_eq!(s.route("GET", "/nope", "").0, 404);
}
