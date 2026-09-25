//! 进程内崩溃恢复测试助手：写入确认后等待 SIGKILL，或重开验证全部累计数据。

use std::collections::BTreeSet;
use std::io::{self, Write};

use fuju_trace_engine::{parse_wire_batch, WriteCoordinator};

fn main() {
    let mut args = std::env::args().skip(1);
    let action = args.next().expect("expected write or verify");
    let dir = args.next().expect("expected data directory");
    let round: u64 = args
        .next()
        .expect("expected round")
        .parse()
        .expect("round must be an integer");
    assert!(round > 0);

    let coord = WriteCoordinator::open_durable(&dir).expect("open durable trace store");
    coord.recover();
    match action.as_str() {
        "write" => {
            let batch = format!(
                r#"[{{"trace_id":1,"span_id":{round},"ts":{round},"seq":1,"event_type":3,"ext_span_id":"1-{round}","status":0,"input_tokens":100,"agent_name":"风控","logs":["第{round}轮 灌入"]}}]"#
            );
            let records = parse_wire_batch(&batch).expect("valid test event");
            coord.try_ingest_wire(records).expect("durable WAL append");
            println!("READY");
            io::stdout().flush().expect("flush ready marker");
            std::thread::park();
        }
        "verify" => {
            let snap = coord.pin_snapshot();
            let expected: BTreeSet<u64> = (1..=round).collect();
            let spans: BTreeSet<u64> = coord
                .read_spans(&snap)
                .into_iter()
                .filter(|span| span.trace_id == 1)
                .map(|span| span.span_id)
                .collect();
            assert_eq!(spans, expected, "recovered span IDs");
            let hits: BTreeSet<u64> = coord
                .search_text(&snap, "灌入", round as usize)
                .into_iter()
                .filter(|(span, _)| span.trace_id == 1)
                .map(|(span, _)| span.span_id)
                .collect();
            assert_eq!(hits, expected, "recovered search IDs");
            println!("round {round}: all spans and search results recovered");
        }
        _ => panic!("expected write or verify"),
    }
}
