import { useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useTrace } from '../hooks/queries'
import { spanDisplayName, type Span } from '../api'
import { TraceFlow } from './TraceFlow'
import { TurnTimeline } from './TurnTimeline'

type View = 'wf' | 'flow'

const fmtDur = (ms: number) => (ms >= 1000 ? (ms / 1000).toFixed(2) + 's' : ms + 'ms')
const spanDur = (s: Span) => s.status === 'run' ? '进行中' : s.status === 'incomplete' ? '不完整' : fmtDur(s.durMs)
const KIND_LABEL: Record<string, string> = { llm: 'LLM', tool: 'TOOL', chain: 'CHAIN', retriever: 'RETR', agent: 'AGENT', other: 'SPAN' }

export function Waterfall({
  sessionId,
  traceId,
  selectedSpan,
  onSelectSpan,
  onSelectTurn,
}: {
  sessionId: string | null
  traceId: string | null
  selectedSpan: string | null
  onSelectSpan: (id: string) => void
  onSelectTurn: (traceId: string) => void
}) {
  const { data, isLoading } = useTrace(traceId)
  const spans = data?.spans ?? []
  const summary = data?.summary
  const [view, setView] = useState<View>('wf')
  // 聚焦模式：null = 全部；否则只看该 span 的子树，时间轴按子树窗口重缩放。
  const [focusId, setFocusId] = useState<string | null>(null)

  const byParent = useMemo(() => {
    const map = new Map<string | null, Span[]>()
    for (const s of spans) {
      const key = s.parentId ?? null
      if (!map.has(key)) map.set(key, [])
      map.get(key)!.push(s)
    }
    return map
  }, [spans])

  const focusSpan = focusId ? spans.find((s) => s.id === focusId) ?? null : null

  // 聚焦子树（含自身）；沿 parentId 向上取面包屑路径。
  const { visible, breadcrumb } = useMemo(() => {
    if (!focusSpan) return { visible: spans, breadcrumb: [] as Span[] }
    const byId = new Map(spans.map((s) => [s.id, s]))
    const out: Span[] = []
    const walk = (id: string) => {
      const node = byId.get(id)
      if (!node) return
      out.push(node)
      for (const child of byParent.get(id) ?? []) walk(child.id)
    }
    walk(focusSpan.id)
    const path: Span[] = []
    let cur: Span | undefined = focusSpan
    while (cur) {
      path.unshift(cur)
      cur = cur.parentId ? byId.get(cur.parentId) : undefined
    }
    return { visible: out, breadcrumb: path }
  }, [focusSpan, spans, byParent])

  const baseDepth = focusSpan?.depth ?? 0
  const maxEnd = useMemo(() => Math.max(1, ...visible.map((s) => s.startMs + s.durMs)), [visible])
  const agg = useMemo(() => {
    let inTok = 0, outTok = 0, cost = 0
    for (const s of visible) { inTok += s.inTok ?? 0; outTok += s.outTok ?? 0; cost += s.cost }
    return { inTok, outTok, cost, tot: inTok + outTok }
  }, [visible])

  const parentRef = useRef<HTMLDivElement>(null)
  const virt = useVirtualizer({
    count: visible.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 30,
    overscan: 20,
  })

  const clickRow = (s: Span) => {
    if (selectedSpan === s.id) {
      // 再点已选行：切换聚焦（对齐「点开看子调用」的直觉）。
      setFocusId(focusId === s.id ? null : s.id)
    }
    onSelectSpan(s.id)
  }

  return (
    /* 中栏：顶部横向轮次条（点 chip 切轮次）+ 选中轮次的火焰图 / 步骤流 / Agent 图。 */
    <div className="cright">
      <div className="turnbar">
        <TurnTimeline sessionId={sessionId} selectedTrace={traceId} onSelect={onSelectTurn} />
      </div>
      {!traceId ? (
        <div className="empty">← 从时间线或会话列表选一轮</div>
      ) : (
        <>
            <div className="toolbar">
              <div>
                <div className="tt">{focusSpan ? spanDisplayName(focusSpan) : summary?.name ?? '加载中…'}</div>
                <div className="sub">{traceId}{summary ? ` · ${summary.spanCount} spans · ${fmtDur(summary.durMs)}` : ''}</div>
              </div>
              <div className="seg">
                <button className={view === 'wf' ? 'on' : ''} onClick={() => setView('wf')}>火焰图</button>
                <button className={view === 'flow' ? 'on' : ''} onClick={() => setView('flow')}>轨迹图</button>
              </div>
            </div>
            {focusSpan && (
              /* 聚焦面包屑：全部 › … › 当前；点任意层回退，点「全部」退出聚焦。 */
              <div className="fbcrumb">
                <button onClick={() => setFocusId(null)}>全部步骤</button>
                {breadcrumb.map((s) => (
                  <span className="fseg" key={s.id}>
                    <span className="fsep">›</span>
                    <button className={s.id === focusId ? 'cur' : ''} onClick={() => setFocusId(s.id)} title={s.name}>
                      <span className={'kind k-' + s.kind}>{KIND_LABEL[s.kind]}</span> {spanDisplayName(s)}
                    </button>
                  </span>
                ))}
              </div>
            )}
            {focusSpan && (
              /* 聚焦指标网格：当前窗口的耗时 / token / 成本 / 子调用数。 */
              <div className="fgrid">
                <div className="fcell"><span>窗口耗时</span><strong>{fmtDur(focusSpan.durMs)}</strong></div>
                <div className="fcell" title={agg.tot ? undefined : '窗口内没有 LLM 调用，未产生 token'}><span>窗口 Token</span><strong>{agg.tot ? agg.tot.toLocaleString() : '无 LLM'}</strong></div>
                <div className="fcell"><span>窗口成本</span><strong>${agg.cost.toFixed(4)}</strong></div>
                <div className="fcell"><span>子调用</span><strong>{visible.length - 1}</strong></div>
              </div>
            )}
            {view === 'wf' && !focusSpan && (
              <div className="summary">
                <div className="blk"><span className="lab">总 Token</span><span className="big">{agg.tot.toLocaleString()}</span></div>
                <div className="blk"><span className="lab">成本</span><span className="big cost">${agg.cost.toFixed(3)}</span></div>
                <div className="blk"><span className="lab">Span</span><span className="big">{visible.length.toLocaleString()}</span></div>
              </div>
            )}
            {view === 'wf' && (
              <div className="ruler">
                {Array.from({ length: 6 }, (_, i) => (
                  <span key={i}>{((maxEnd / 6) * i / 1000).toFixed(1)}s</span>
                ))}
              </div>
            )}
            {isLoading ? (
              <div className="spin">加载 span…</div>
            ) : view === 'wf' ? (
              <div className="wfwrap" ref={parentRef}>
                <div className="vinner" style={{ height: virt.getTotalSize() }}>
                  {virt.getVirtualItems().map((vi) => (
                    <div key={vi.key} className="vrow" style={{ transform: `translateY(${vi.start}px)`, height: 30 }}>
                      <SpanRow
                        s={visible[vi.index]}
                        maxEnd={maxEnd}
                        depth={visible[vi.index].depth - baseDepth}
                        sel={selectedSpan === visible[vi.index].id}
                        focused={focusId === visible[vi.index].id}
                        onClick={() => clickRow(visible[vi.index])}
                      />
                    </div>
                  ))}
                </div>
              </div>
            ) : view === 'flow' ? (
              <TraceFlow spans={visible} selectedSpan={selectedSpan} onSelectSpan={onSelectSpan} />
            ) : null}
            {view === 'wf' && !focusSpan && spans.length > 1 && <Insight spans={spans} onSelect={onSelectSpan} />}
          </>
        )}
    </div>
  )
}

// 本 trace 洞察：耗时 / 成本 Top5（从内存里全部 span 算，与虚拟化渲染无关）。点行选中。
function Insight({ spans, onSelect }: { spans: Span[]; onSelect: (id: string) => void }) {
  const completed = spans.filter((s) => s.lifecycle === 'completed' || (s.lifecycle == null && s.status !== 'run' && s.status !== 'incomplete'))
  const byDur = [...completed].sort((a, b) => b.durMs - a.durMs).slice(0, 5)
  const byCost = [...spans].sort((a, b) => b.cost - a.cost).slice(0, 5)
  const maxDur = Math.max(1, ...byDur.map((s) => s.durMs))
  const maxCost = Math.max(1e-6, ...byCost.map((s) => s.cost))
  const row = (s: Span, i: number, frac: number, val: string, color: string) => (
    <div className="irow" key={s.id} onClick={() => onSelect(s.id)} title={spanDisplayName(s)}>
      <span className="irank">{i + 1}</span>
      <span className="iname">{spanDisplayName(s)}</span>
      <span className="imeter"><i style={{ width: Math.max(frac * 100, 5) + '%', background: color }} /></span>
      <span className="ival">{val}</span>
    </div>
  )
  return (
    <div className="insight">
      <div className="icard">
        <h5>⏱ 耗时 Top 5</h5>
        {byDur.map((s, i) => row(s, i, s.durMs / maxDur, fmtDur(s.durMs), `var(--${s.kind === 'retriever' ? 'retr' : s.kind})`))}
      </div>
      <div className="icard">
        <h5>💰 成本 Top 5</h5>
        {byCost.map((s, i) => row(s, i, s.cost / maxCost, '$' + s.cost.toFixed(3), 'var(--ok)'))}
      </div>
    </div>
  )
}

function SpanRow({ s, maxEnd, depth, sel, focused, onClick }: {
  s: Span
  maxEnd: number
  depth: number
  sel: boolean
  focused: boolean
  onClick: () => void
}) {
  const left = (s.startMs / maxEnd) * 100
  const w = Math.max((s.durMs / maxEnd) * 100, 0.5)
  const barcls = s.status === 'error' ? 'b-err' : s.status === 'run' ? 'b-run' : s.status === 'incomplete' ? 'b-incomplete' : 'b-' + s.kind
  return (
    <div className={'srow' + (sel ? ' sel' : '') + (focused ? ' focus' : '')} onClick={onClick} title="单击选中，再点一次聚焦子调用">
      <div className="sname" style={{ paddingLeft: 14 + depth * 12 }}>
        <span className={'kind k-' + s.kind}>{KIND_LABEL[s.kind]}</span>
        <span className="slabel">{spanDisplayName(s)}{s.status === 'error' && <span className="errchip"> ERR</span>}{s.status === 'run' && <span className="runchip"> RUN</span>}{s.status === 'incomplete' && <span className="incompletechip"> INCOMPLETE</span>}</span>
      </div>
      <div className="wf"><div className={'bar ' + barcls} style={{ left: left + '%', width: w + '%' }} /></div>
      <div className="sdur">{spanDur(s)}</div>
    </div>
  )
}
