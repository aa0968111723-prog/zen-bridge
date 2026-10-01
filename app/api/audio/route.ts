export const dynamic = 'force-dynamic';
import { z } from 'zod';
import { one, rows, id, saveSegment, translateText, failure } from '@/lib/server';
import { getEnv } from '@/lib/env';
import { putAudio } from '@/lib/data';
import { resolveSpeech } from '@/lib/speech/resolve';
import { glossary } from '@/lib/speech/glossary';
import type { Memory, Person, Session } from '@/lib/domain';

export async function POST(request: Request) {
  try {
    if (Number(request.headers.get('content-length') ?? 0) > 6_000_000) throw new Error('單段錄音請小於 5 MB。');
    const form = await request.formData(), file = form.get('audio');
    const direction = z.enum(['zh-en', 'en-zh']).parse(form.get('direction') ?? 'zh-en');
    const sessionId = z.string().uuid().parse(form.get('sessionId'));
    const auto = form.get('auto') === 'true';
    const personId = z.string().uuid().nullable().parse(form.get('personId') || null);
    const role = String(form.get('role') ?? '講者').slice(0, 80), offset = Number(form.get('offset') ?? 0);
    if (!(file instanceof File) || file.size > 5_000_000 || !file.size || !Number.isFinite(offset) || offset < 0 || offset > 86400) throw new Error('錄音資料格式不正確。');
    const session = await one<Session>('SELECT * FROM sessions WHERE id=?', sessionId);
    if (!session) throw new Error('找不到活動。');
    const person = personId ? await one<Person>('SELECT * FROM people WHERE id=?', personId) : null;
    if (personId && !person) throw new Error('找不到講者。');
    const env = getEnv(), provider = resolveSpeech(env);
    const audio = new Uint8Array(await file.arrayBuffer());
    const audioKey = await putAudio('recordings/' + sessionId + '/' + id(), audio, file.type || 'audio/wav');
    const memories = await rows<Memory>("SELECT * FROM memories WHERE status='verified' ORDER BY updated_at DESC");
    const terms = glossary(memories, direction, env.HOTWORD_LIMIT);
    const parts = await provider.transcribe({ audio, filename: file.name, mime: file.type, direction,
      phrases: terms.phrases, topic: session.topic, speakerNote: person?.notes ?? '', auto,
      speakerKey: person?.id ?? null, knownSpeakerIds: JSON.parse(session.speaker_ids) });
    const ids: string[] = [], roles: Record<string, string> = JSON.parse(session.roles);
    for (const part of parts) {
      const source = part.source.trim();
      let translation = part.translation.trim();
      if (!source && !translation) continue;
      const detected = part.speakerKey ? (part.speakerKey === person?.id ? person : await one<Person>('SELECT * FROM people WHERE id=?', part.speakerKey)) : null;
      const currentRole = auto ? roles[detected?.id ?? ''] ?? detected?.role ?? '待確認' : role;
      const notes = [part.note, terms.note].filter(Boolean);
      if (source && !translation && env.OPENAI_API_KEY) {
        try { translation = await translateText(source, session, detected, currentRole, direction); }
        catch { notes.push('補譯失敗，原文已保存；請檢查 OPENAI_API_KEY 與 OPENAI_TRANSLATION_MODEL。'); }
      }
      ids.push(await saveSegment({ sessionId, personId: detected?.id ?? null,
        label: detected?.name ?? (direction === 'en-zh' ? '英文發言者' : '待確認講者'), role: currentRole,
        direction, zh: direction === 'zh-en' ? source : translation, en: direction === 'en-zh' ? source : translation,
        note: notes.join('。'), source: auto ? 'automatic' : 'microphone', audioKey, offset: offset + part.offset }));
    }
    if (!ids.length) throw new Error('這一段沒有辨識出文字');
    return Response.json({ ids, provider: provider.name });
  } catch (error) { return failure(error); }
}
