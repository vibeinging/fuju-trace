// Rust 的 u64 ID 必须在 JSON.parse 之前保留，reviver 拿到的 number 已丢精度。
// 字符串 token 整体跳过，避免把日志/提示词里的数字改写。
export function parseLosslessJson(text: string): unknown {
  const safe = text.replace(/"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, token => {
    if (/^-?\d+$/.test(token) && !Number.isSafeInteger(Number(token))) return JSON.stringify(token)
    return token
  })
  return JSON.parse(safe)
}
