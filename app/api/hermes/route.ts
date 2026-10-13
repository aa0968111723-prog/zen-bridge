export const dynamic = 'force-dynamic';
import { checkHermesConnection } from '@/lib/hermes';

const headers = { 'Cache-Control': 'no-store' };

export async function GET() {
  try {
    const connected = await checkHermesConnection();
    return Response.json({ ...connected, available: true }, { headers });
  } catch (error) {
    const reason = error instanceof Error ? error.message : 'Hermes 目前不可用。';
    return Response.json({ status: 'unavailable', available: false, reason }, { headers });
  }
}
