import assert from 'node:assert/strict'
import test from 'node:test'
import { parseLosslessJson } from '../src/api/json.ts'

test('exact u64 trace/span IDs survive the wire boundary, including old records', () => {
  const row = parseLosslessJson('{"trace_id":884939838166405120,"span_id":18446744073709551615,"score":1.25,"status":0}')
  assert.equal(row.trace_id, '884939838166405120')
  assert.equal(row.span_id, '18446744073709551615')
  assert.equal(row.score, 1.25)
  assert.equal(row.status, 0)
})

test('numbers inside trace text and escaped strings remain byte-for-byte text', () => {
  const text = '记录 18446744073709551615; "trace_id":884939838166405120; \\path\\'
  assert.deepEqual(parseLosslessJson(JSON.stringify({text, array: [1, null, '9007199254740993']})), {text, array: [1, null, '9007199254740993']})
  assert.throws(() => parseLosslessJson('{broken}'))
})
