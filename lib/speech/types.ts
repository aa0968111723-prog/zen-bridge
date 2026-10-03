import type { Direction } from '../domain';
export type SpeechInput = {
  audio: Uint8Array;
  filename: string;
  mime: string;
  direction: Direction;
  phrases: Record<string, string>;
  topic: string;
  speakerNote: string;
  auto: boolean;
  speakerKey?: string | null;
  knownSpeakerIds?: string[];
};
export type SpeechPart = {
  source: string;
  translation: string;
  note: string;
  provider: 'breeze' | 'qwen-live' | 'openai';
  speakerKey: string | null;
  offset: number;
};
export interface SpeechProvider {
  readonly name: SpeechPart['provider'];
  transcribe(input: SpeechInput): Promise<SpeechPart[]>;
}
