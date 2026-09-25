#!/usr/bin/env python3
"""Reproducible Fuju Trace VexDB adapter benchmark on disposable tables.

This is an end-to-end client benchmark: Python folding, SQL round trips,
transactions, native indexes and result hydration are all in the timed path.
It deliberately does not call this a pure database-engine benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO / "fuju-trace-sdk" / "python"))

from fuju_trace_vexdb import VexDBTraceStore

TENANT = 42
PROJECTS = ("scale-a", "scale-b", "scale-c", "scale-d")


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct / 100
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def distribution(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min_ms": round(min(values), 3),
        "p50_ms": round(percentile(values, 50), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "p99_ms": round(percentile(values, 99), 3),
        "max_ms": round(max(values), 3),
    }


def make_events(spans: int, text_chars: int, prefix: str) -> list[dict[str, Any]]:
    pad = ("交易核查与工具调用记录" * ((text_chars + 10) // 11))[:text_chars]
    events = []
    for i in range(spans):
        trace_id, span_id = i // 10 + 1, i % 10 + 1
        project = PROJECTS[(trace_id - 1) % len(PROJECTS)]
        failed = i % 13 == 0
        common = i % 5 == 0
        rare = i % 997 == 0
        base = {
            "trace_id": trace_id, "span_id": span_id,
            "ext_span_id": f"{prefix}-{trace_id}-{span_id}",
            "parent_span_id": None if span_id == 1 else span_id // 2,
            "session_id": 10_000 + (trace_id - 1) // 4,
            "tenant_id": TENANT,
        }
        ts = 1_720_000_000_000_000_000 + i * 1_000_000
        events.append({
            **base, "ts": ts, "seq": 1, "event_type": 1,
            "agent_name": "risk-agent", "tool_name": "risk-check",
            "input_text": f"检查交易 {pad}" + (" 支付风控" if common else ""),
            "input_tokens": 200 + i % 100,
            "attrs": {"project_id": project, "task_fingerprint": "risk-review", "skill": "risk-check"},
        })
        events.append({
            **base, "ts": ts + 500_000, "seq": 2, "event_type": 4,
            "logs": ["疑似盗刷，工具超时，待重试" if common else "工具调用完成"],
        })
        end = {
            **base, "ts": ts + 900_000, "seq": 3, "event_type": 2,
            "status": 1 if failed else 0, "duration_ns": 900_000,
            "output_text": ("月蚀校验码 " if rare else "")
                + ("支付风控失败，需要人工复核" if failed else "处理完成")
                + f" {pad}",
            "output_tokens": 50 + i % 30,
        }
        events.append(end)
        if i % 100 == 0:
            events.append(dict(end))  # intentional retry
    return events


def make_vectors(spans: int, dim: int) -> list[list[float]]:
    rng = random.Random(20260925)
    out = []
    for _ in range(spans):
        raw = [rng.uniform(-1, 1) for _ in range(dim)]
        norm = math.sqrt(sum(value * value for value in raw))
        out.append([value / norm for value in raw])
    return out


def drop_tables(dsn: str, prefix: str, psycopg2: Any) -> None:
    connection = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with connection.cursor() as cur:
            for suffix in ("attrs", "events", "spans", "config"):
                cur.execute(f"DROP TABLE IF EXISTS {prefix}_{suffix}")
        connection.commit()
    finally:
        connection.close()


def database_info(dsn: str, psycopg2: Any) -> dict[str, Any]:
    connection = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            samples = []
            for _ in range(21):
                start = time.perf_counter_ns()
                cur.execute("SELECT 1")
                cur.fetchone()
                samples.append((time.perf_counter_ns() - start) / 1e6)
        connection.rollback()
        return {"server_version": version, "round_trip_ms": distribution(samples[1:]), "round_trip_samples_ms": samples[1:]}
    finally:
        connection.close()


def write_events(store: Any, events: list[dict[str, Any]], batch: int, spans: int, *, expected_ingested: int | None = None) -> dict[str, Any]:
    latencies = []
    ingested = 0
    start = time.perf_counter()
    for offset in range(0, len(events), batch):
        tick = time.perf_counter_ns()
        ingested += store.ingest(events[offset:offset + batch])["ingested"]
        latencies.append((time.perf_counter_ns() - tick) / 1e6)
    elapsed = time.perf_counter() - start
    expected = spans * 3 if expected_ingested is None else expected_ingested
    if ingested != expected:
        raise AssertionError(f"expected {expected} reported ingested events, got {ingested}")
    return {
        "seconds": round(elapsed, 6), "spans_per_second": round(spans / elapsed, 2),
        "reported_events_per_second": round(ingested / elapsed, 2),
        "submitted_events": len(events), "reported_ingested_events": ingested,
        "batch_size": batch, "batch_latency": distribution(latencies),
        "batch_samples_ms": [round(value, 3) for value in latencies],
    }


def embed_vectors(store: VexDBTraceStore, vectors: list[list[float]], count: int, batch: int) -> dict[str, Any]:
    latencies = []
    start = time.perf_counter()
    for offset in range(0, count, batch):
        tick = time.perf_counter_ns()
        if batch == 1:
            if not store.set_embedding(offset // 10 + 1, offset % 10 + 1, vectors[offset]):
                raise AssertionError(f"missing span for vector {offset}")
        else:
            rows = [(i // 10 + 1, i % 10 + 1, vectors[i])
                    for i in range(offset, min(offset + batch, count))]
            changed = store.set_embeddings(rows)
            if changed != len(rows):
                raise AssertionError(f"expected {len(rows)} vectors updated, got {changed}")
        latencies.append((time.perf_counter_ns() - tick) / 1e6)
    elapsed = time.perf_counter() - start
    return {
        "count": count, "seconds": round(elapsed, 6), "batch_size": batch,
        "vectors_per_second": round(count / elapsed, 2) if count else None,
        "latency": distribution(latencies),
        "samples_ms": [round(value, 3) for value in latencies],
    }


def exact_cosine_topk(vectors: list[list[float]], query: list[float], indices: list[int], k: int) -> list[int]:
    query_norm = math.sqrt(sum(value * value for value in query))
    scored = []
    for i in indices:
        vector = vectors[i]
        dot = sum(a * b for a, b in zip(vector, query))
        norm = math.sqrt(sum(value * value for value in vector))
        scored.append((1 - dot / (norm * query_norm), i))
    return [i for _, i in sorted(scored)[:k]]


def hit_index(hit: dict[str, Any]) -> int:
    return (int(hit["trace_id"]) - 1) * 10 + int(hit["span_id"]) - 1


def measure_queries(cases: dict[str, Callable[[], Any]], repeats: int, seed: int = 42) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}
    for name, query in cases.items():
        tick = time.perf_counter_ns()
        hits = query()
        first_ms = (time.perf_counter_ns() - tick) / 1e6
        if not isinstance(hits, list):
            raise AssertionError(f"{name} did not return a list")
        results[name] = {"first_ms": round(first_ms, 3), "hits": len(hits)}
    for query in cases.values():
        query()  # warm all paths once before timed interleaving
    rng = random.Random(seed)
    samples = {name: [] for name in cases}
    for _ in range(repeats):
        names = list(cases)
        rng.shuffle(names)
        for name in names:
            tick = time.perf_counter_ns()
            cases[name]()
            samples[name].append((time.perf_counter_ns() - tick) / 1e6)
    for name in cases:
        results[name]["warm_latency"] = distribution(samples[name])
        results[name]["warm_samples_ms"] = [round(value, 3) for value in samples[name]]
    return results


def query_plans(store: VexDBTraceStore, vector: list[float]) -> dict[str, Any]:
    plans = {}
    encoded = "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"
    statements = {
        "bm25": (f"SELECT s.trace_id, s.span_id, bm25_score() AS score FROM {store.prefix}_spans s "
                  "WHERE s.tenant_id=%s AND s.search_text @~@ %s ORDER BY score DESC NULLS LAST LIMIT 10", (str(TENANT), "盗刷")),
        "vector": (f"SELECT s.trace_id, s.span_id FROM {store.prefix}_spans s "
                   "WHERE s.tenant_id=%s AND s.embedding IS NOT NULL "
                   "ORDER BY s.embedding <=> %s::floatvector LIMIT 10", (str(TENANT), encoded)),
        "vector_project": (f"SELECT s.trace_id, s.span_id FROM {store.prefix}_spans s "
                           f"WHERE s.tenant_id=%s AND s.embedding IS NOT NULL AND EXISTS (SELECT 1 FROM {store.prefix}_attrs a "
                           "WHERE a.tenant_id=s.tenant_id AND a.trace_id=s.trace_id AND a.span_id=s.span_id "
                           "AND a.attr_key=%s AND a.value_json=%s) "
                           "ORDER BY s.embedding <=> %s::floatvector LIMIT 10", (str(TENANT), "project_id", '"scale-a"', encoded)),
    }
    for name, (sql, params) in statements.items():
        try:
            with store._tx() as cur:
                cur.execute("EXPLAIN " + sql, params)
                plans[name] = [str(row[0]) for row in cur.fetchall()]
        except Exception as exc:
            plans[name] = {"error": type(exc).__name__, "message": str(exc)[:250]}
    return plans


def relation_bytes(store: VexDBTraceStore) -> dict[str, Any]:
    sizes: dict[str, Any] = {"tables": {}, "indexes": {}}
    for suffix in ("events", "spans", "attrs"):
        try:
            with store._tx() as cur:
                cur.execute("SELECT pg_relation_size(%s::regclass), pg_total_relation_size(%s::regclass)",
                            (f"{store.prefix}_{suffix}", f"{store.prefix}_{suffix}"))
                heap, total = cur.fetchone()
                sizes["tables"][suffix] = {"heap_bytes": int(heap), "total_bytes": int(total)}
        except Exception:
            sizes["tables"][suffix] = None
    for suffix in ("event_span_idx", "attrs_lookup_idx", "span_time_idx", "text_idx", "vector_idx"):
        try:
            with store._tx() as cur:
                cur.execute("SELECT pg_relation_size(%s::regclass)", (f"{store.prefix}_{suffix}",))
                sizes["indexes"][suffix] = int(cur.fetchone()[0])
        except Exception:
            sizes["indexes"][suffix] = None
    return sizes


def embedded_baseline(events: list[dict[str, Any]], batch: int, spans: int, package_dir: str, repeats: int) -> dict[str, Any]:
    sys.path.insert(0, package_dir)
    from fuju_trace_db import FujuTraceDB, _native
    native_path = str(Path(_native.__file__).resolve())
    if not native_path.startswith(str(Path(package_dir).resolve()) + os.sep):
        raise RuntimeError(f"embedded native binary was not loaded from {package_dir}")
    with tempfile.TemporaryDirectory(prefix="fuju-vexdb-embedded-") as tmp:
        tick = time.perf_counter()
        db = FujuTraceDB.open(tmp, tenant_id=TENANT)
        try:
            write = write_events(db, events, batch, spans, expected_ingested=len(events))
        finally:
            db.close()
        write["open_write_close_seconds"] = round(time.perf_counter() - tick, 6)
        disk_bytes = sum(path.stat().st_size for path in Path(tmp).rglob("*") if path.is_file())
        db = FujuTraceDB.open(tmp, tenant_id=TENANT)
        try:
            cases = {
                "span_point": lambda: [db.span(1, 1)],
                "trace_10_spans": lambda: [db.trace(1)],
                "bm25_common": lambda: db.search(text="盗刷", k=10),
                "bm25_rare": lambda: db.search(text="月蚀校验码", k=10),
                "bm25_project_failure": lambda: db.search(text="盗刷", k=10,
                    filter={"status": 1, "attrs": {"project_id": "scale-a"}}),
            }
            queries = measure_queries(cases, repeats)
        finally:
            db.close()
        return {"native_path": native_path, "native_sha256": hashlib.sha256(Path(native_path).read_bytes()).hexdigest(),
                "write": write, "disk_bytes": disk_bytes, "queries": queries}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spans", type=int, default=1000)
    parser.add_argument("--single-spans", type=int, default=100)
    parser.add_argument("--batch", type=int, default=128)
    parser.add_argument("--vector-dim", type=int, default=128)
    parser.add_argument("--vector-count", type=int, default=-1, help="-1 means one per span")
    parser.add_argument("--embedding-batch", type=int, default=1,
                        help="vectors per set_embeddings call; 1 uses set_embedding")
    parser.add_argument("--text-chars", type=int, default=120)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--embedded-package-dir", default="")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    if not 1 <= args.spans <= 5000 or not 0 <= args.single_spans <= 500:
        parser.error("spans must be 1..5000 and single-spans 0..500")
    if min(args.batch, args.vector_dim, args.text_chars, args.queries, args.embedding_batch) < 1:
        parser.error("batch, vector-dim, text-chars, queries and embedding-batch must be positive")
    vector_count = args.spans if args.vector_count < 0 else args.vector_count
    if not 0 <= vector_count <= args.spans:
        parser.error("vector-count must be between 0 and spans")
    dsn = os.environ.get("VEXDB_DSN")
    if not dsn:
        parser.error("set VEXDB_DSN locally; the report never records it")
    import psycopg2

    report: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "client": {"python": sys.version.split()[0], "platform": platform.platform(),
                   "psycopg2": psycopg2.__version__, "database_location": "remote; address omitted"},
        "config": {"spans": args.spans, "single_spans": args.single_spans,
                   "batch": args.batch, "vector_dim": args.vector_dim,
                   "vector_count": vector_count, "embedding_batch": args.embedding_batch,
                   "text_chars": args.text_chars,
                   "queries_per_case": args.queries, "tenant": TENANT,
                   "dataset": "synthetic agent risk triage; 3 events/span, 3 attrs/span, 1% retry"},
        "database": database_info(dsn, psycopg2),
    }
    try:
        report["git_head"] = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:
        report["git_head"] = None

    prefix = "ft_bench_" + report["run_id"]
    events = make_events(args.spans, args.text_chars, prefix)
    vectors = make_vectors(args.spans, args.vector_dim)
    query_vector = vectors[min(vector_count - 1, max(0, vector_count // 2))] if vector_count else None
    store = None
    print(f"benchmark prefix={prefix} spans={args.spans} vectors={vector_count}", flush=True)
    try:
        tick = time.perf_counter()
        store = VexDBTraceStore.open(dsn, tenant_id=TENANT, vector_dim=args.vector_dim,
                                      table_prefix=prefix, initialize=True)
        report["vexdb_setup_seconds"] = round(time.perf_counter() - tick, 6)
        print(f"initialized in {report['vexdb_setup_seconds']} s", flush=True)
        report["vexdb_write"] = write_events(store, events, args.batch, args.spans)
        print(f"batch write {report['vexdb_write']['seconds']} s", flush=True)
        report["vexdb_embedding"] = embed_vectors(store, vectors, vector_count, args.embedding_batch)
        print(f"embeddings {report['vexdb_embedding']['seconds']} s", flush=True)
        report["vexdb_relation_bytes"] = relation_bytes(store)
        cases = {
            "span_point": lambda: [store.span(1, 1)],
            "trace_10_spans": lambda: store.trace(1),
            "bm25_common": lambda: store.search(text="盗刷", k=10),
            "bm25_rare": lambda: store.search(text="月蚀校验码", k=10),
            "bm25_project_failure": lambda: store.search(text="盗刷", k=10,
                filter={"status": 1, "attrs": {"project_id": "scale-a"}}),
        }
        if query_vector is not None:
            project = PROJECTS[((vector_count // 2) // 10) % len(PROJECTS)]
            cases.update({
                "vector": lambda: store.search(vector=query_vector, k=10),
                "vector_project": lambda: store.search(vector=query_vector, k=10,
                    filter={"attrs": {"project_id": project}}),
                "hybrid": lambda: store.search(text="盗刷", vector=query_vector, k=10),
            })
        report["vexdb_queries"] = measure_queries(cases, args.queries)
        if query_vector is not None:
            nearest = store.search(vector=query_vector, k=10)
            exact = exact_cosine_topk(vectors, query_vector, list(range(vector_count)), 10)
            project = PROJECTS[((vector_count // 2) // 10) % len(PROJECTS)]
            selected = [i for i in range(vector_count) if PROJECTS[(i // 10) % len(PROJECTS)] == project]
            filtered = store.search(vector=query_vector, k=10, filter={"attrs": {"project_id": project}})
            exact_filtered = exact_cosine_topk(vectors, query_vector, selected, 10)
            report["recall_at_10"] = {
                "vector": len({hit_index(hit) for hit in nearest} & set(exact)) / min(10, len(exact)),
                "vector_project": len({hit_index(hit) for hit in filtered} & set(exact_filtered)) / min(10, len(exact_filtered)),
                "vector_hits": len(nearest), "vector_project_hits": len(filtered),
                "project": project,
            }
            report["vexdb_plans"] = query_plans(store, query_vector)
        print("queries completed", flush=True)
    finally:
        if store is not None:
            store.close()
        drop_tables(dsn, prefix, psycopg2)
        print("temporary VexDB tables removed", flush=True)

    if args.single_spans:
        prefix_single = "ft_bench_" + uuid.uuid4().hex[:12]
        single_events = make_events(args.single_spans, args.text_chars, prefix_single)
        store = None
        try:
            store = VexDBTraceStore.open(dsn, tenant_id=TENANT, vector_dim=args.vector_dim,
                                          table_prefix=prefix_single, initialize=True)
            report["vexdb_single_event_write"] = write_events(store, single_events, 1, args.single_spans)
            print(f"single-event write {report['vexdb_single_event_write']['seconds']} s", flush=True)
        finally:
            if store is not None:
                store.close()
            drop_tables(dsn, prefix_single, psycopg2)
            print("single-event tables removed", flush=True)

    if args.embedded_package_dir:
        print("running local embedded baseline", flush=True)
        report["embedded_baseline"] = embedded_baseline(events, args.batch, args.spans,
                                                          args.embedded_package_dir, args.queries)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"report saved to {args.report}", flush=True)
    print(json.dumps({
        "vexdb_write": report["vexdb_write"]["spans_per_second"],
        "vexdb_vector": report["vexdb_embedding"]["vectors_per_second"],
        "vexdb_bm25_p50_ms": report["vexdb_queries"]["bm25_common"]["warm_latency"]["p50_ms"],
        "vexdb_vector_p50_ms": report["vexdb_queries"].get("vector", {}).get("warm_latency", {}).get("p50_ms"),
        "recall_at_10": report.get("recall_at_10"),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
