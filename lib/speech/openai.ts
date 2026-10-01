import type { AppEnv } from '../env';
import { getAudio, rows } from '../data';
import type { Person } from '../domain';
import type { SpeechProvider } from './types';
export function openaiProvider(env: AppEnv): SpeechProvider {
  return {
    name: 'openai',
    async transcribe(input) {
      const out = new FormData();
      out.set('file', new File([input.audio as Uint8Array<ArrayBuffer>], input.filename, { type: input.mime }));
      out.set('model', input.auto ? 'gpt-4o-transcribe-diarize' : env.OPENAI_TRANSCRIPTION_MODEL);
      const matched: string[] = [];
      if (input.auto) {
        out.set('response_format', 'diarized_json');
        out.set('chunking_strategy', 'auto');
        const people = await rows<Person>('SELECT * FROM people WHERE reference_key IS NOT NULL ORDER BY created_at');
        const selected = input.knownSpeakerIds ?? [];
        const known = (selected.length ? people.filter(p => selected.includes(p.id)) : people).slice(0, 4);
        for (const person of known) {
          const object = await getAudio(person.reference_key!);
          if (!object) continue;
          const bytes = new Uint8Array(await object.arrayBuffer());
          let binary = '';
          for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
          out.append('known_speaker_names[]', person.id);
          out.append('known_speaker_references[]', 'data:' + (person.reference_type ?? object.contentType) + ';base64,' + btoa(binary));
          matched.push(person.id);
        }
      } else {
        out.set('response_format', 'json');
        out.append('languages[]', input.direction === 'en-zh' ? 'en' : 'zh-tw');
        out.set('prompt', 'Tamkang University Zen Club / 淡江禪學社。主題：' + input.topic.slice(0, 250) + '。' + input.speakerNote.slice(0, 400));
      }
      let result: Response;
      try {
        result = await fetch('https://api.openai.com/v1/audio/transcriptions', { method: 'POST', headers: { Authorization: 'Bearer ' + env.OPENAI_API_KEY }, body: out, signal: AbortSignal.timeout(55000) });
      } catch { throw new Error('OpenAI 語音辨識連線失敗，請檢查平台網路與 OPENAI_API_KEY。'); }
      if (!result.ok) throw new Error(result.status === 429 ? '語音服務額度不足或忙碌，請稍後重試。' : 'OpenAI 語音辨識連線失敗，請檢查 OPENAI_API_KEY。');
      let body: { text?: string; segments?: { text: string; speaker: string; start: number }[] };
      try { body = await result.json() as typeof body; }
      catch { throw new Error('OpenAI 語音服務回傳格式不正確，請檢查 OPENAI_TRANSCRIPTION_MODEL。'); }
      const parts = input.auto ? body.segments ?? [] : [{ text: body.text ?? '', speaker: input.speakerKey ?? '', start: 0 }];
      return parts.map(part => ({ source: part.text?.trim() ?? '', translation: '', note: '', provider: 'openai' as const,
        speakerKey: input.auto ? matched.includes(part.speaker) ? part.speaker : null : input.speakerKey ?? null,
        offset: Number.isFinite(part.start) && part.start >= 0 ? part.start : 0 }));
    },
  };
}
