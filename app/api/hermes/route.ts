export const dynamic = 'force-dynamic';
import { checkHermesConnection } from '@/lib/hermes';
import { failure } from '@/lib/server';

export async function GET() {
  try { return Response.json(await checkHermesConnection(), { headers: { 'Cache-Control': 'no-store' } }); }
  catch (error) { return failure(error); }
}
