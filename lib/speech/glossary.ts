import type { Memory, Direction } from '../domain';
export function glossary(memories: Pick<Memory, 'status' | 'zh' | 'en'>[], direction: Direction, limit = 200) {
  const n = !Number.isFinite(limit) || limit < 1 ? 200 : Math.min(1000, Math.floor(limit));
  const phrases: Record<string, string> = Object.create(null);
  for (const memory of memories) {
    if (memory.status !== 'verified') continue;
    const source = (direction === 'zh-en' ? memory.zh : memory.en).trim();
    const target = (direction === 'zh-en' ? memory.en : memory.zh).trim();
    if (!source || !target || Array.from(source).length > 40 || Array.from(target).length > 80) continue;
    phrases[source] = target;
    if (Object.keys(phrases).length >= n) break;
  }
  return { phrases, note: `熱詞 ${Object.keys(phrases).length} 條，不是模型微調` };
}
