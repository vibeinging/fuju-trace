import { useTurns } from '../hooks/queries'

const fmtDur = (ms: number) => (ms >= 1000 ? (ms / 1000).toFixed(2) + 's' : ms + 'ms')

// 横向轮次条：选中会话的各轮横排 chip，嵌在火焰图顶部，点 chip 切轮次。
// 复用 useTurns（切会话自动复用缓存）。纯内容渲染，宿主提供 .turnbar 外壳。
export function TurnTimeline({
  sessionId,
  selectedTrace,
  onSelect,
}: {
  sessionId: string | null
  selectedTrace: string | null
  onSelect: (traceId: string) => void
}) {
  const { data: turns, isLoading } = useTurns(sessionId, !!sessionId)

  if (!sessionId) return <span className="tlempty">← 从左侧选一条会话</span>
  if (isLoading) return <span className="tlempty">加载轮次…</span>
  if (!turns || turns.length === 0) return <span className="tlempty">该会话无轮次数据</span>

  return (
    <>
      {turns.map((t) => {
        const tot = (t.inTok ?? 0) + (t.outTok ?? 0)
        const sel = selectedTrace === t.traceId
        const meta = `${fmtDur(t.durMs)}${tot ? ` · ${tot} tok` : ''}${t.status === 'error' ? ' · 出错' : ''}`
        return (
          <div
            key={t.traceId}
            className={'tchip' + (sel ? ' sel' : '')}
            onClick={() => onSelect(t.traceId)}
            title={`第${t.turnIndex + 1}轮 · ${t.name} · ${meta}`}
          >
            <span className="tlno">{t.turnIndex + 1}</span>
            <span className="tlname">{t.name}</span>
          </div>
        )
      })}
    </>
  )
}
