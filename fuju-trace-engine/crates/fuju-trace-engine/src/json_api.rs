//! 进程内 JSON 调用层，供嵌入式 Python、Node 和 Rust 适配器复用。
//!
//! 不打开网络端口；method/path 只是本地调用的稳定分派键。
#![allow(dead_code)]

use std::sync::Arc;

use crate::{
    parse_wire_batch, AnnotationStatus, AnnotationTarget, DatasetAssociation,
    DatasetAssociationFilter, NewDatasetAssociation, NewTraceAnnotation, Projection, ReadPlanStats,
    SearchFilter, TraceAnnotation, TraceAnnotationFilter, TraceQuery, UpdateTraceAnnotation,
    WriteCoordinator,
};
use fuju_trace_core::fold::FoldedSpan;

#[derive(Clone)]
pub struct EngineJsonApi {
    coord: Arc<WriteCoordinator>,
}

impl EngineJsonApi {
    pub fn new(coord: Arc<WriteCoordinator>) -> Self {
        Self { coord }
    }

    /// 纯路由（无 socket，便于单测和嵌入式调用）。返回 (status, json_body)。
    pub fn route(&self, method: &str, path: &str, body: &str) -> (u16, String) {
        self.route_with_tenant(method, path, body, None)
    }

    /// 带租户上下文的进程内路由：检索/列表端点据此强制隔离。
    pub fn route_with_tenant(
        &self,
        method: &str,
        path: &str,
        body: &str,
        tenant: Option<u64>,
    ) -> (u16, String) {
        // 切掉查询串：精确路由按 base 匹配，查询参数（分页 cursor/limit）单独解析。
        let (base, query) = path.split_once('?').unwrap_or((path, ""));
        match (method, base) {
            ("POST", "/v1/ingest") => match parse_wire_batch(body) {
                Ok(recs) => {
                    let n = recs.len();
                    match self.coord.try_ingest_wire_for_tenant(recs, tenant) {
                        Ok(_) => (200, format!(r#"{{"ingested":{n}}}"#)),
                        Err(_) => (500, r#"{"error":"write failed"}"#.to_string()),
                    }
                }
                Err(e) => (400, format!(r#"{{"error":"{}"}}"#, e.replace('"', "'"))),
            },
            ("GET", "/v1/traces") => (200, self.traces_json(tenant)),
            // 进程内检索：中文 BM25 + 可选属性过滤(agent/状态/时间/trace) + 租户隔离。
            ("POST", "/v1/search") => self.search_json(body, tenant),
            // 单机读模型：先保证 trace 级过滤、聚合和空间统计可用；高性能索引后续单独迁移。
            ("POST", "/v1/trace-search") => self.trace_search_json(body, tenant),
            ("POST", "/v1/trace-aggregate") => self.trace_aggregate_json(body, tenant),
            ("POST", "/v1/storage-stats") => self.storage_stats_json(body, tenant),
            ("POST", "/v1/retention-plan") | ("POST", "/v1/retention/plan") => {
                self.retention_plan_json(body, tenant, false)
            }
            ("POST", "/v1/retention/apply") => self.retention_plan_json(body, tenant, true),
            ("GET", "/v1/retention-audits") | ("GET", "/v1/retention/audits") => {
                self.retention_audits_query_json(query, tenant)
            }
            ("POST", "/v1/retention-audits") | ("POST", "/v1/retention/audits") => {
                self.retention_audits_body_json(body, tenant)
            }
            ("POST", "/v1/retention-policies") | ("POST", "/v1/retention/policies") => {
                self.create_retention_policy_json(body, tenant)
            }
            ("GET", "/v1/retention-policies") | ("GET", "/v1/retention/policies") => {
                self.retention_policies_query_json(query, tenant)
            }
            ("POST", "/v1/retention-policies/run-due")
            | ("POST", "/v1/retention/policies/run-due")
            | ("POST", "/v1/retention/run-due") => {
                self.run_due_retention_policies_json(body, tenant)
            }
            ("POST", "/v1/trace-trajectories") => self.trace_trajectories_json(body, tenant),
            ("POST", "/v1/trajectory-groups") => self.trajectory_groups_json(body, tenant),
            ("POST", "/v1/traces/diff") => self.trace_diff_json(body, tenant),
            ("POST", "/v1/annotations") => self.create_annotation_json(body, tenant),
            ("GET", "/v1/annotations") => self.annotations_json(query, tenant),
            ("POST", "/v1/dataset-associations") | ("POST", "/v1/dataset-links") => {
                self.create_dataset_association_json(body, tenant)
            }
            ("GET", "/v1/dataset-associations") | ("GET", "/v1/dataset-links") => {
                self.dataset_associations_json(query, tenant)
            }
            // 嵌入式详情查询：会话游标分页 / 轮次 / trace span / span 详情。
            ("GET", "/v1/sessions") => (200, self.sessions_page_json(query, tenant)),
            ("GET", "/v1/loops") => (200, self.loops_page_json(query, tenant)),
            _ => self.route_detail(method, base, query, body, tenant),
        }
    }

    /// 带路径参数的进程内详情调用（/v1/sessions/:id/turns 等）。
    fn route_detail(
        &self,
        method: &str,
        base: &str,
        query: &str,
        body: &str,
        tenant: Option<u64>,
    ) -> (u16, String) {
        let segs: Vec<&str> = base
            .trim_start_matches('/')
            .split('/')
            .filter(|s| !s.is_empty())
            .collect();
        match (method, segs.as_slice()) {
            ("GET", ["v1", "sessions", id, "turns"]) => self.turns_json(id, tenant),
            ("GET", ["v1", "traces", id]) => self.trace_json(id, tenant),
            ("GET", ["v1", "traces", id, "steps"]) => self.steps_json(id, tenant),
            ("GET", ["v1", "traces", id, "spans", sid]) => self.span_detail_json(id, sid, tenant),
            ("GET", ["v1", "loops", loop_id]) => self.loop_detail_json(loop_id, query, tenant),
            ("PATCH", ["v1", "annotations", id]) => self.update_annotation_json(id, body, tenant),
            ("POST", ["v1", "annotations", id, "status"]) => {
                self.update_annotation_json(id, body, tenant)
            }
            ("DELETE", ["v1", "annotations", id]) => self.delete_annotation_json(id, body, tenant),
            ("GET", ["v1", "tasks", fingerprint, "traces"]) => {
                (200, self.task_traces_json(fingerprint, query, tenant))
            }
            _ => (404, r#"{"error":"not found"}"#.to_string()),
        }
    }

    /// 处理 `POST /v1/search`：body = `{"text":"盗刷","vector":[..],"k":10,"filter":{"agent_name":"风控"}}`。
    /// 按给了什么自动选检索路:只 text→中文检索;只 vector→找相似;两个都给→混合(RRF)。都按 filter 过滤。
    fn search_json(&self, body: &str, tenant: Option<u64>) -> (u16, String) {
        use crate::wire::{field, parse, Json};
        let v = match parse(body) {
            Ok(v) => v,
            Err(e) => return (400, format!(r#"{{"error":"{}"}}"#, e.replace('"', "'"))),
        };
        let text = field(&v, "text").and_then(Json::as_str).unwrap_or("");
        let k = field(&v, "k").and_then(Json::as_u64).unwrap_or(10) as usize;
        let vector: Vec<f32> = field(&v, "vector")
            .map(|j| j.as_array().iter().filter_map(Json::as_f32).collect())
            .unwrap_or_default();
        let mut filter = crate::SearchFilter::default();
        if let Some(f) = field(&v, "filter") {
            let trace_id_value = json_field_alias(f, &["trace_id", "traceId"]);
            filter.external_trace_id =
                json_field_alias(f, &["external_trace_id", "externalTraceId"])
                    .and_then(Json::as_str)
                    .map(str::to_string)
                    .or_else(|| trace_id_value.and_then(json_id_text));
            filter.agent_name = field(f, "agent_name")
                .and_then(Json::as_str)
                .map(|s| s.to_string());
            filter.tool_name = json_field_alias(f, &["tool_name", "toolName"])
                .and_then(Json::as_str)
                .map(|s| s.to_string());
            filter.model = field(f, "model").and_then(Json::as_str).map(str::to_string);
            filter.status = field(f, "status").and_then(Json::as_u64).map(|x| x as u8);
            filter.time_from = field(f, "time_from").and_then(Json::as_i64);
            filter.time_to = field(f, "time_to").and_then(Json::as_i64);
            collect_attr_filters(f, &mut filter);
        }
        // 租户来自宿主传入的调用上下文，覆盖请求体。
        filter.tenant_id = tenant;

        let snap = self.coord.pin_snapshot();
        let hits = match (!text.is_empty(), !vector.is_empty()) {
            (true, true) => self
                .coord
                .search_hybrid_attr(&snap, text, &vector, k, &filter), // 混合
            (false, true) => self.coord.search_similar_attr(&snap, &vector, k, &filter), // 找相似
            _ => self.coord.search_text_attr(&snap, text, k, &filter),                   // 中文检索
        };
        let items: Vec<String> = hits
            .iter()
            .map(|(s, score)| {
                let logs: Vec<String> = s.logs.iter().map(|l| format!("\"{}\"", json_escape(l))).collect();
                format!(
                    r#"{{"trace_id":{},"span_id":{},"external_trace_id":{},"external_span_id":{},"score":{:.4},"status":{},"duration_ns":{},"agent_name":{},"logs":[{}],"attrs":{}}}"#,
                    s.trace_id,
                    s.span_id,
                    json_opt_str(s.external_trace_id.as_deref()),
                    json_opt_str(s.external_span_id.as_deref()),
                    score,
                    s.status.map_or("null".to_string(), |x| x.to_string()),
                    s.duration_ns.map_or("null".to_string(), |x| x.to_string()),
                    s.agent_name.as_ref().map_or("null".to_string(), |a| format!("\"{}\"", json_escape(a))),
                    logs.join(","),
                    json_attrs(&s.attrs),
                )
            })
            .collect();
        (200, format!("[{}]", items.join(",")))
    }

    fn traces_json(&self, tenant: Option<u64>) -> String {
        let snap = self.coord.pin_snapshot();
        let mut q = TraceQuery::all();
        q.tenant_id = tenant; // 租户隔离：只列本租户的 trace
        let traces = self.coord.list_traces(&snap, &q);
        let items: Vec<String> = traces
            .iter()
            .map(|t| {
                format!(
                    r#"{{"trace_id":{},"external_trace_id":{},"span_count":{},"total_duration_ns":{},"max_duration_ns":{},"error_count":{},"total_input_tokens":{},"total_output_tokens":{},"total_cache_read_tokens":{},"total_cache_write_tokens":{},"cache_read_reported_spans":{},"cache_write_reported_spans":{},"total_llm_spans":{}}}"#,
                    t.trace_id,
                    json_opt_str(t.external_trace_id.as_deref()),
                    t.span_count,
                    t.total_duration_ns,
                    t.max_duration_ns,
                    t.error_count,
                    t.total_input_tokens,
                    t.total_output_tokens,
                    t.total_cache_read_tokens.map_or("null".to_string(), |v| v.to_string()),
                    t.total_cache_write_tokens.map_or("null".to_string(), |v| v.to_string()),
                    t.cache_read_reported_spans,
                    t.cache_write_reported_spans,
                    t.total_llm_spans,
                )
            })
            .collect();
        format!("[{}]", items.join(","))
    }

    // ───────────────────── 进程内详情查询（游标分页 / 轮次 / span / 详情） ─────────────────────
}

include!("json_api/read_model_api.rs");
include!("json_api/metadata_api.rs");
include!("json_api/path_api.rs");
include!("json_api/detail_api.rs");

include!("json_api/json_helpers.rs");
include!("json_api/trace_filter_helpers.rs");
include!("json_api/metadata_helpers.rs");
include!("json_api/read_model_helpers.rs");
include!("json_api/trajectory_helpers.rs");
include!("json_api/json_escape.rs");
include!("json_api/retention_helpers.rs");
include!("json_api/retention_api.rs");

#[cfg(test)]
mod tests;
