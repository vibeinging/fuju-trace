use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub type Result<T> = std::result::Result<T, FujuTraceError>;

#[derive(Debug)]
pub enum FujuTraceError {
    Io(std::io::Error),
    InvalidUrl(String),
}

impl fmt::Display for FujuTraceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FujuTraceError::Io(err) => write!(f, "{err}"),
            FujuTraceError::InvalidUrl(message) => write!(f, "{message}"),
        }
    }
}

impl std::error::Error for FujuTraceError {}

impl From<std::io::Error> for FujuTraceError {
    fn from(value: std::io::Error) -> Self {
        FujuTraceError::Io(value)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum EventType {
    SpanStart = 1,
    SpanEnd = 2,
    Attr = 3,
    Log = 4,
    Error = 5,
}

impl EventType {
    pub fn tag(self) -> u8 {
        self as u8
    }
}

const FNV_OFFSET: u64 = 0xcbf29ce484222325;
const FNV_PRIME: u64 = 0x100000001b3;

pub fn event_id(ext_span_id: &str, seq: u64, event_type: EventType) -> u64 {
    let mut hash = FNV_OFFSET;
    for byte in ext_span_id
        .as_bytes()
        .iter()
        .copied()
        .chain(seq.to_le_bytes())
        .chain([event_type.tag()])
    {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(FNV_PRIME);
    }
    hash
}

#[derive(Clone, Debug)]
pub struct SpanEvent {
    pub trace_id: u64,
    pub span_id: u64,
    pub ts: i64,
    pub seq: u64,
    pub event_type: EventType,
    pub ext_span_id: String,
    pub parent_span_id: Option<u64>,
    pub status: Option<u8>,
    pub duration_ns: Option<u64>,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub cache_read_tokens: Option<u64>,
    pub cache_write_tokens: Option<u64>,
    pub session_id: Option<u64>,
    pub tenant_id: Option<u64>,
    pub span_name: Option<String>,
    pub display_name: Option<String>,
    pub agent_name: Option<String>,
    pub tool_name: Option<String>,
    pub model: Option<String>,
    pub input_text: Option<String>,
    pub output_text: Option<String>,
    pub logs: Vec<String>,
}

impl SpanEvent {
    pub fn event_id(&self) -> u64 {
        event_id(&self.ext_span_id, self.seq, self.event_type)
    }

    pub fn to_json(&self) -> String {
        let mut fields = vec![
            format!("\"trace_id\":{}", self.trace_id),
            format!("\"span_id\":{}", self.span_id),
            format!("\"ts\":{}", self.ts),
            format!("\"seq\":{}", self.seq),
            format!("\"event_type\":{}", self.event_type.tag()),
            format!("\"ext_span_id\":{}", json_string(&self.ext_span_id)),
            format!("\"event_id\":{}", self.event_id()),
        ];
        push_opt_num(&mut fields, "parent_span_id", self.parent_span_id);
        push_opt_num(&mut fields, "status", self.status);
        push_opt_num(&mut fields, "duration_ns", self.duration_ns);
        push_opt_num(&mut fields, "input_tokens", self.input_tokens);
        push_opt_num(&mut fields, "output_tokens", self.output_tokens);
        push_opt_num(&mut fields, "cache_read_tokens", self.cache_read_tokens);
        push_opt_num(&mut fields, "cache_write_tokens", self.cache_write_tokens);
        push_opt_num(&mut fields, "session_id", self.session_id);
        push_opt_num(&mut fields, "tenant_id", self.tenant_id);
        push_opt_str(&mut fields, "span_name", self.span_name.as_deref());
        push_opt_str(&mut fields, "display_name", self.display_name.as_deref());
        push_opt_str(&mut fields, "agent_name", self.agent_name.as_deref());
        push_opt_str(&mut fields, "tool_name", self.tool_name.as_deref());
        push_opt_str(&mut fields, "model", self.model.as_deref());
        push_opt_str(&mut fields, "input_text", self.input_text.as_deref());
        push_opt_str(&mut fields, "output_text", self.output_text.as_deref());
        fields.push(format!(
            "\"logs\":[{}]",
            self.logs
                .iter()
                .map(|value| json_string(value))
                .collect::<Vec<_>>()
                .join(",")
        ));
        format!("{{{}}}", fields.join(","))
    }
}

pub fn events_to_json(events: &[SpanEvent]) -> String {
    format!(
        "[{}]",
        events
            .iter()
            .map(SpanEvent::to_json)
            .collect::<Vec<_>>()
            .join(",")
    )
}

pub trait Exporter {
    fn export(&mut self, event: SpanEvent) -> Result<()>;

    fn export_batch(&mut self, events: Vec<SpanEvent>) -> Result<()> {
        for event in events {
            self.export(event)?;
        }
        Ok(())
    }

    fn close(&mut self) -> Result<()> {
        Ok(())
    }
}

#[derive(Default)]
pub struct ConsoleExporter;

impl Exporter for ConsoleExporter {
    fn export(&mut self, event: SpanEvent) -> Result<()> {
        println!("{}", event.to_json());
        Ok(())
    }
}

#[derive(Default)]
pub struct CollectingExporter {
    events: Vec<SpanEvent>,
}

impl CollectingExporter {
    pub fn events(&self) -> &[SpanEvent] {
        &self.events
    }

    pub fn into_events(self) -> Vec<SpanEvent> {
        self.events
    }
}

impl Exporter for CollectingExporter {
    fn export(&mut self, event: SpanEvent) -> Result<()> {
        self.events.push(event);
        Ok(())
    }
}

pub struct BatchExporter<E> {
    sink: E,
    max_batch: usize,
    buffer: Vec<SpanEvent>,
}

impl<E: Exporter> BatchExporter<E> {
    pub fn new(sink: E, max_batch: usize) -> Self {
        Self {
            sink,
            max_batch: max_batch.max(1),
            buffer: Vec::new(),
        }
    }

    pub fn flush(&mut self) -> Result<()> {
        if self.buffer.is_empty() {
            return Ok(());
        }
        let batch = std::mem::take(&mut self.buffer);
        self.sink.export_batch(batch)
    }

    pub fn into_inner(mut self) -> Result<E> {
        self.flush()?;
        Ok(self.sink)
    }
}

impl<E: Exporter> Exporter for BatchExporter<E> {
    fn export(&mut self, event: SpanEvent) -> Result<()> {
        self.buffer.push(event);
        if self.buffer.len() >= self.max_batch {
            self.flush()?;
        }
        Ok(())
    }

    fn close(&mut self) -> Result<()> {
        self.flush()?;
        self.sink.close()
    }
}

pub struct Tracer<E = ConsoleExporter> {
    exporter: E,
    ids: Snowflake,
}

impl Tracer<ConsoleExporter> {
    pub fn new() -> Self {
        Self::with_exporter(ConsoleExporter, 0)
    }
}

impl Default for Tracer<ConsoleExporter> {
    fn default() -> Self {
        Self::new()
    }
}

impl<E: Exporter> Tracer<E> {
    pub fn with_exporter(exporter: E, node_id: u16) -> Self {
        Self {
            exporter,
            ids: Snowflake::new(node_id),
        }
    }

    pub fn trace<T>(
        &mut self,
        name: impl Into<String>,
        run: impl FnOnce(&mut Trace<'_, E>) -> T,
    ) -> Result<T> {
        self.trace_with(name, TraceOptions::default(), run)
    }

    pub fn trace_with<T>(
        &mut self,
        name: impl Into<String>,
        options: TraceOptions,
        run: impl FnOnce(&mut Trace<'_, E>) -> T,
    ) -> Result<T> {
        let trace_id = self.ids.next();
        let mut trace = Trace {
            tracer: self,
            trace_id,
            name: name.into(),
            session_id: options.session_id,
            tenant_id: options.tenant_id,
            agent_name: options.agent_name,
        };
        Ok(run(&mut trace))
    }

    pub fn trace_result<T>(
        &mut self,
        name: impl Into<String>,
        run: impl FnOnce(&mut Trace<'_, E>) -> Result<T>,
    ) -> Result<T> {
        self.trace_with_result(name, TraceOptions::default(), run)
    }

    pub fn trace_with_result<T>(
        &mut self,
        name: impl Into<String>,
        options: TraceOptions,
        run: impl FnOnce(&mut Trace<'_, E>) -> Result<T>,
    ) -> Result<T> {
        let trace_id = self.ids.next();
        let mut trace = Trace {
            tracer: self,
            trace_id,
            name: name.into(),
            session_id: options.session_id,
            tenant_id: options.tenant_id,
            agent_name: options.agent_name,
        };
        run(&mut trace)
    }

    pub fn close(&mut self) -> Result<()> {
        self.exporter.close()
    }

    pub fn into_exporter(self) -> E {
        self.exporter
    }

    fn emit(&mut self, event: SpanEvent) -> Result<()> {
        self.exporter.export(event)
    }
}

#[derive(Default, Clone, Debug)]
pub struct TraceOptions {
    session_id: Option<u64>,
    tenant_id: Option<u64>,
    agent_name: Option<String>,
}

impl TraceOptions {
    pub fn session_id(mut self, session_id: u64) -> Self {
        self.session_id = Some(session_id);
        self
    }

    pub fn tenant_id(mut self, tenant_id: u64) -> Self {
        self.tenant_id = Some(tenant_id);
        self
    }

    pub fn agent_name(mut self, agent_name: impl Into<String>) -> Self {
        self.agent_name = Some(agent_name.into());
        self
    }
}

pub struct Trace<'a, E> {
    tracer: &'a mut Tracer<E>,
    trace_id: u64,
    name: String,
    session_id: Option<u64>,
    tenant_id: Option<u64>,
    agent_name: Option<String>,
}

impl<E: Exporter> Trace<'_, E> {
    pub fn span<T>(
        &mut self,
        name: impl Into<String>,
        run: impl FnOnce(&mut Span<'_, E>) -> T,
    ) -> Result<T> {
        self.span_with(name, SpanOptions::default(), run)
    }

    pub fn span_with<T>(
        &mut self,
        name: impl Into<String>,
        options: SpanOptions,
        run: impl FnOnce(&mut Span<'_, E>) -> T,
    ) -> Result<T> {
        let span_id = self.tracer.ids.next();
        let mut span = Span::new(
            self.tracer,
            self.trace_id,
            span_id,
            name.into(),
            options.parent_span_id,
            self.session_id,
            self.tenant_id,
            options
                .agent_name
                .clone()
                .or_else(|| self.agent_name.clone()),
            options.display_name.clone(),
        );
        span.tool_name = options.tool_name;
        span.model = options.model;
        span.input_text = options.input_text;
        span.start()?;
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run(&mut span)));
        if result.is_err() {
            span.status = Some(1);
        }
        let end_result = span.end();
        match result {
            Ok(value) => {
                end_result?;
                Ok(value)
            }
            Err(payload) => {
                let _ = end_result;
                std::panic::resume_unwind(payload);
            }
        }
    }

    pub fn span_result<T>(
        &mut self,
        name: impl Into<String>,
        run: impl FnOnce(&mut Span<'_, E>) -> Result<T>,
    ) -> Result<T> {
        self.span_with_result(name, SpanOptions::default(), run)
    }

    pub fn span_with_result<T>(
        &mut self,
        name: impl Into<String>,
        options: SpanOptions,
        run: impl FnOnce(&mut Span<'_, E>) -> Result<T>,
    ) -> Result<T> {
        let span_id = self.tracer.ids.next();
        let mut span = Span::new(
            self.tracer,
            self.trace_id,
            span_id,
            name.into(),
            options.parent_span_id,
            self.session_id,
            self.tenant_id,
            options
                .agent_name
                .clone()
                .or_else(|| self.agent_name.clone()),
            options.display_name.clone(),
        );
        span.tool_name = options.tool_name;
        span.model = options.model;
        span.input_text = options.input_text;
        span.start()?;
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run(&mut span)));
        match result {
            Ok(Ok(value)) => {
                span.end()?;
                Ok(value)
            }
            Ok(Err(err)) => {
                span.status = Some(1);
                let _ = span.end();
                Err(err)
            }
            Err(payload) => {
                span.status = Some(1);
                let _ = span.end();
                std::panic::resume_unwind(payload);
            }
        }
    }

    pub fn trace_id(&self) -> u64 {
        self.trace_id
    }

    pub fn name(&self) -> &str {
        &self.name
    }
}

pub struct Span<'a, E> {
    tracer: &'a mut Tracer<E>,
    trace_id: u64,
    span_id: u64,
    name: String,
    parent_span_id: Option<u64>,
    ext_span_id: String,
    seq: u64,
    status: Option<u8>,
    input_tokens: Option<u64>,
    output_tokens: Option<u64>,
    cache_read_tokens: Option<u64>,
    cache_write_tokens: Option<u64>,
    session_id: Option<u64>,
    tenant_id: Option<u64>,
    display_name: Option<String>,
    agent_name: Option<String>,
    tool_name: Option<String>,
    model: Option<String>,
    input_text: Option<String>,
    output_text: Option<String>,
    start_ns: Option<i64>,
}

impl<'a, E: Exporter> Span<'a, E> {
    fn new(
        tracer: &'a mut Tracer<E>,
        trace_id: u64,
        span_id: u64,
        name: String,
        parent_span_id: Option<u64>,
        session_id: Option<u64>,
        tenant_id: Option<u64>,
        agent_name: Option<String>,
        display_name: Option<String>,
    ) -> Self {
        Self {
            tracer,
            trace_id,
            span_id,
            name,
            parent_span_id,
            ext_span_id: format!("{trace_id}-{span_id}"),
            seq: 0,
            status: None,
            input_tokens: None,
            output_tokens: None,
            cache_read_tokens: None,
            cache_write_tokens: None,
            session_id,
            tenant_id,
            display_name: display_name
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty()),
            agent_name,
            tool_name: None,
            model: None,
            input_text: None,
            output_text: None,
            start_ns: None,
        }
    }

    pub fn span<T>(
        &mut self,
        name: impl Into<String>,
        run: impl FnOnce(&mut Span<'_, E>) -> T,
    ) -> Result<T> {
        self.span_with(name, SpanOptions::default(), run)
    }

    pub fn span_with<T>(
        &mut self,
        name: impl Into<String>,
        options: SpanOptions,
        run: impl FnOnce(&mut Span<'_, E>) -> T,
    ) -> Result<T> {
        let child_id = self.tracer.ids.next();
        let mut child = Span::new(
            self.tracer,
            self.trace_id,
            child_id,
            name.into(),
            Some(self.span_id),
            self.session_id,
            self.tenant_id,
            options
                .agent_name
                .clone()
                .or_else(|| self.agent_name.clone()),
            options.display_name.clone(),
        );
        child.tool_name = options.tool_name;
        child.model = options.model;
        child.input_text = options.input_text;
        child.start()?;
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run(&mut child)));
        if result.is_err() {
            child.status = Some(1);
        }
        let end_result = child.end();
        match result {
            Ok(value) => {
                end_result?;
                Ok(value)
            }
            Err(payload) => {
                let _ = end_result;
                std::panic::resume_unwind(payload);
            }
        }
    }

    pub fn span_result<T>(
        &mut self,
        name: impl Into<String>,
        run: impl FnOnce(&mut Span<'_, E>) -> Result<T>,
    ) -> Result<T> {
        self.span_with_result(name, SpanOptions::default(), run)
    }

    pub fn span_with_result<T>(
        &mut self,
        name: impl Into<String>,
        options: SpanOptions,
        run: impl FnOnce(&mut Span<'_, E>) -> Result<T>,
    ) -> Result<T> {
        let child_id = self.tracer.ids.next();
        let mut child = Span::new(
            self.tracer,
            self.trace_id,
            child_id,
            name.into(),
            Some(self.span_id),
            self.session_id,
            self.tenant_id,
            options
                .agent_name
                .clone()
                .or_else(|| self.agent_name.clone()),
            options.display_name.clone(),
        );
        child.tool_name = options.tool_name;
        child.model = options.model;
        child.input_text = options.input_text;
        child.start()?;
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run(&mut child)));
        match result {
            Ok(Ok(value)) => {
                child.end()?;
                Ok(value)
            }
            Ok(Err(err)) => {
                child.status = Some(1);
                let _ = child.end();
                Err(err)
            }
            Err(payload) => {
                child.status = Some(1);
                let _ = child.end();
                std::panic::resume_unwind(payload);
            }
        }
    }

    pub fn log(&mut self, message: impl Into<String>) -> Result<()> {
        self.emit(EventType::Log, None, None, vec![message.into()])
    }

    pub fn set_status(&mut self, status: u8) {
        self.status = Some(status);
    }

    pub fn set_tokens(&mut self, input_tokens: Option<u64>, output_tokens: Option<u64>) {
        if let Some(value) = input_tokens {
            self.input_tokens = Some(value);
        }
        if let Some(value) = output_tokens {
            self.output_tokens = Some(value);
        }
    }

    pub fn set_cache_tokens(
        &mut self,
        cache_read_tokens: Option<u64>,
        cache_write_tokens: Option<u64>,
    ) {
        if let Some(value) = cache_read_tokens {
            self.cache_read_tokens = Some(value);
        }
        if let Some(value) = cache_write_tokens {
            self.cache_write_tokens = Some(value);
        }
    }

    pub fn set_agent(&mut self, value: impl Into<String>) {
        self.agent_name = Some(value.into());
    }

    pub fn set_tool(&mut self, value: impl Into<String>) {
        self.tool_name = Some(value.into());
    }

    pub fn set_model(&mut self, value: impl Into<String>) {
        self.model = Some(value.into());
    }

    pub fn set_io(&mut self, input_text: Option<String>, output_text: Option<String>) {
        if let Some(value) = input_text {
            self.input_text = Some(value);
        }
        if let Some(value) = output_text {
            self.output_text = Some(value);
        }
    }

    pub fn span_id(&self) -> u64 {
        self.span_id
    }

    fn start(&mut self) -> Result<()> {
        let started = now_ns();
        self.start_ns = Some(started);
        self.emit(EventType::SpanStart, None, None, Vec::new())
    }

    fn end(&mut self) -> Result<()> {
        let ended = now_ns();
        let duration_ns = ended.saturating_sub(self.start_ns.unwrap_or(ended)).max(0) as u64;
        self.emit(
            EventType::SpanEnd,
            self.status,
            Some(duration_ns),
            Vec::new(),
        )
    }

    fn next_seq(&mut self) -> u64 {
        self.seq += 1;
        self.seq
    }

    fn emit(
        &mut self,
        event_type: EventType,
        status: Option<u8>,
        duration_ns: Option<u64>,
        logs: Vec<String>,
    ) -> Result<()> {
        let is_end = event_type == EventType::SpanEnd;
        let event = SpanEvent {
            trace_id: self.trace_id,
            span_id: self.span_id,
            ts: now_ns(),
            seq: self.next_seq(),
            event_type,
            ext_span_id: self.ext_span_id.clone(),
            parent_span_id: self.parent_span_id,
            status,
            duration_ns,
            input_tokens: is_end.then_some(self.input_tokens).flatten(),
            output_tokens: is_end.then_some(self.output_tokens).flatten(),
            cache_read_tokens: is_end.then_some(self.cache_read_tokens).flatten(),
            cache_write_tokens: is_end.then_some(self.cache_write_tokens).flatten(),
            session_id: self.session_id,
            tenant_id: self.tenant_id,
            span_name: (event_type == EventType::SpanStart).then(|| self.name.clone()),
            display_name: (event_type == EventType::SpanStart)
                .then(|| self.display_name.clone())
                .flatten(),
            agent_name: self.agent_name.clone(),
            tool_name: self.tool_name.clone(),
            model: self.model.clone(),
            input_text: self.input_text.clone(),
            output_text: self.output_text.clone(),
            logs,
        };
        self.tracer.emit(event)
    }
}

#[derive(Default, Clone, Debug)]
pub struct SpanOptions {
    parent_span_id: Option<u64>,
    display_name: Option<String>,
    agent_name: Option<String>,
    tool_name: Option<String>,
    model: Option<String>,
    input_text: Option<String>,
}

impl SpanOptions {
    pub fn parent_span_id(mut self, value: u64) -> Self {
        self.parent_span_id = Some(value);
        self
    }

    pub fn display_name(mut self, value: impl Into<String>) -> Self {
        self.display_name = Some(value.into());
        self
    }

    pub fn agent_name(mut self, value: impl Into<String>) -> Self {
        self.agent_name = Some(value.into());
        self
    }

    pub fn tool_name(mut self, value: impl Into<String>) -> Self {
        self.tool_name = Some(value.into());
        self
    }

    pub fn model(mut self, value: impl Into<String>) -> Self {
        self.model = Some(value.into());
        self
    }

    pub fn input_text(mut self, value: impl Into<String>) -> Self {
        self.input_text = Some(value.into());
        self
    }
}

struct Snowflake {
    node_id: u16,
    counter: AtomicU64,
}

impl Snowflake {
    fn new(node_id: u16) -> Self {
        Self {
            node_id: node_id & 0x03ff,
            counter: AtomicU64::new(0),
        }
    }

    fn next(&self) -> u64 {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .min((1u128 << 42) - 1) as u64;
        let seq = self.counter.fetch_add(1, Ordering::Relaxed) & 0x0fff;
        (millis << 22) | (u64::from(self.node_id) << 12) | seq
    }
}

fn now_ns() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(i64::MAX as u128) as i64
}

fn push_opt_str(fields: &mut Vec<String>, key: &str, value: Option<&str>) {
    if let Some(value) = value {
        fields.push(format!("\"{key}\":{}", json_string(value)));
    }
}

fn push_opt_num<T: fmt::Display>(fields: &mut Vec<String>, key: &str, value: Option<T>) {
    if let Some(value) = value {
        fields.push(format!("\"{key}\":{value}"));
    }
}

fn json_string(value: &str) -> String {
    format!("\"{}\"", json_escape(value))
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}
