export type RoomEvent = {
  type: 'final';
  id: string;
  zh: string;
  en: string;
  direction: 'zh-en' | 'en-zh';
  t: number;
};

type Room = { history: RoomEvent[]; listeners: Set<(event: RoomEvent) => void> };
const rooms = new Map<string, Room>();

function room(id: string) {
  let value = rooms.get(id);
  if (!value) {
    value = { history: [], listeners: new Set() };
    rooms.set(id, value);
  }
  return value;
}

export function publishFinal(roomId: string, event: Omit<RoomEvent, 'type' | 't'>) {
  const current = room(roomId);
  const finalized: RoomEvent = { type: 'final', t: Date.now(), ...event };
  current.history.push(finalized);
  current.history = current.history.slice(-200);
  for (const listener of current.listeners) listener(finalized);
  return finalized;
}

export function roomHistory(roomId: string) {
  return room(roomId).history.slice(-40);
}

export function subscribeRoom(roomId: string, listener: (event: RoomEvent) => void) {
  const current = room(roomId);
  current.listeners.add(listener);
  return () => current.listeners.delete(listener);
}

export function roomUrl(base: string | undefined, roomId: string) {
  return base ? `${base}/r/${encodeURIComponent(roomId)}` : '';
}
