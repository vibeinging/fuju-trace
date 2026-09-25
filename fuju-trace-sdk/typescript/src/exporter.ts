// 事件导出。
import { toWire, type SpanEvent } from "./event.js";

export interface Exporter {
  export(e: SpanEvent): void;
  // 一次收一批。能真正批量的传输实现成单次请求；没实现就由调用方逐条转。
  exportBatch?(events: SpanEvent[]): void | Promise<void>;
  close?(): void | Promise<void>;
}

// 打印成 JSON 行（调试用）。
export class ConsoleExporter implements Exporter {
  export(e: SpanEvent): void {
    console.log(JSON.stringify(toWire(e)));
  }
}

// 收集到内存（测试用）。
export class CollectingExporter implements Exporter {
  events: SpanEvent[] = [];
  export(e: SpanEvent): void {
    this.events.push(e);
  }
}

// 攒批再发（批量交给下游 sink）。
export class BatchExporter implements Exporter {
  private sink: Exporter;
  private max: number;
  private buf: SpanEvent[] = [];
  private pending: Promise<void> = Promise.resolve();

  constructor(sink: Exporter, max = 256) {
    this.sink = sink;
    this.max = max;
  }

  export(e: SpanEvent): void {
    this.buf.push(e);
    if (this.buf.length >= this.max) this.flush();
  }

  async flush(): Promise<void> {
    if (this.buf.length === 0) return this.pending;
    const batch = this.buf;
    this.buf = [];
    const send = async () => {
      // 整批一次交下游（sink 能批就批，否则逐条）。
      if (this.sink.exportBatch) await this.sink.exportBatch(batch);
      else for (const e of batch) this.sink.export(e);
    };
    const next = this.pending.then(send);
    this.pending = next;
    void next.catch(() => {});
    return next;
  }

  async close(): Promise<void> {
    await this.flush();
    await this.pending;
    await this.sink.close?.();
  }
}
