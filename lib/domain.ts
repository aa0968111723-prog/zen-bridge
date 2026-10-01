export type Person={id:string;name:string;role:string;notes:string;reference_key:string|null;reference_type:string|null};
export type Session={id:string;title:string;topic:string;notes:string;roles:string;speaker_ids:string;status:string;created_at:string};
export type Direction='zh-en'|'en-zh';
export type Segment={id:string;session_id:string;person_id:string|null;label:string;role:string;direction:Direction;zh:string;en:string;original_zh:string;original_en:string;note:string;audio_key:string|null;offset:number;source:string;created_at:string};
export type Memory={id:string;person_id:string|null;role:string;zh:string;en:string;meaning:string;context:string;status:'candidate'|'verified'|'archived';source_id:string|null;version:number;updated_at:string};
export type State={people:Person[];sessions:Session[];segments:Segment[];memories:Memory[];connection:{openai:boolean;qwen:boolean;speech:boolean;provider:string;asr:string;translation:string;hermes:string;deployTarget:string};sessionId:string|null};
export const roles=['主持人','講者','帶領人','分享者'];
