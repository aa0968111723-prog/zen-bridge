import { getBindings, getEnv } from "../lib/env";
import { drizzle } from "drizzle-orm/d1";
import * as schema from "./schema";

export function getDb() {
  const env = getBindings();
  if (getEnv().DEPLOY_TARGET !== 'cloudflare') throw new Error('DEPLOY_TARGET：Postgres 請使用 lib/data.ts。');
  if (!env.DB) {
    throw new Error(
      "資料庫尚未就緒"
    );
  }

  return drizzle(env.DB, { schema });
}
