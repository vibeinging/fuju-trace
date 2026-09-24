import { useMemo } from 'react'
import { spanDisplayName, type Span } from '../api'

const fmtDur = (ms: number) => (ms >= 1000 ? (ms / 1000).toFixed(2) + 's' : ms + 'ms')
const KIND_LABEL: Record<string, string> = { llm: 'LLM', tool: 'TOOL', chain: 'CHAIN', retriever: 'RETR', agent: 'AGENT', other: 'SPAN' }

const NODE_W = 132
const NODE_H = 48
const GAP_X = 34
const PAD = 20

// 轨迹图：把火焰图平铺直述——全部 span 按执行顺序排成一条横向轨迹线，
// 等距展开（不按时间比例挤压），相邻点位箭头相连，节点下标注起始时间。
// 与火焰图共用 visible 数据流：聚焦某 span 时轨迹同样只画该子树。
export function TraceFlow({
  spans,
  selectedSpan,
  onSelectSpan,
}: {
  spans: Span[]
  selectedSpan: string | null
  onSelectSpan: (id: string) => void
}) {
  const { ordered, width } = useMemo(() => {
    const ordered = [...spans].sort((a, b) =>
      a.startMs - b.startMs || a.durMs - b.durMs || a.id.localeCompare(b.id))
    const width = Math.max(920, ordered.length * (NODE_W + GAP_X) + PAD * 2)
    return { ordered, width }
  }, [spans])

  if (ordered.length === 0) return <div className="empty">无 span</div>
  const height = NODE_H + 46

  return (
    <div className="flowwrap">
      <div className="flowcanvas" style={{ width, height }}>
        <svg className="flowedges" width={width} height={height}>
          {ordered.slice(0, -1).map((s, i) => {
            const x1 = PAD + i * (NODE_W + GAP_X) + NODE_W
            const x2 = PAD + (i + 1) * (NODE_W + GAP_X)
            const y = height / 2 - 10
            return (
              <g key={s.id}>
                <line x1={x1 + 2} y1={y} x2={x2 - 6} y2={y} className="feseq" />
                <path d={`M ${x2 - 6} ${y - 3.5} L ${x2} ${y} L ${x2 - 6} ${y + 3.5}`} className="fearrow" />
              </g>
            )
          })}
        </svg>
        {ordered.map((s, i) => {
          const sel = selectedSpan === s.id
          const x = PAD + i * (NODE_W + GAP_X)
          return (
            <div
              key={s.id}
              className={'fnode k-' + s.kind + (sel ? ' sel' : '') + (s.status === 'error' ? ' ferr' : '')}
              style={{ left: x, top: (height - NODE_H) / 2 - 8, width: NODE_W, height: NODE_H }}
              onClick={() => onSelectSpan(s.id)}
              title={spanDisplayName(s)}
            >
              <span className="fbody">
                <span className={'kind k-' + s.kind}>{KIND_LABEL[s.kind]}</span>
                <span className="fnm">{spanDisplayName(s)}</span>
              </span>
              <span className="fmeta">{(s.startMs / 1000).toFixed(1)}s · {fmtDur(s.durMs)}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
