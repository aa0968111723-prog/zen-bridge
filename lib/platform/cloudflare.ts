// Build-time replacement for the Node-only module. No Postgres driver, disk IO,
// or Node WebSocket package is included in the Worker bundle.
const unavailable = () => { throw new Error('DEPLOY_TARGET 不支援此操作。'); };
export const queryPostgres = unavailable;
export const batchPostgres = unavailable;
export const putVolumeAudio = unavailable;
export const getVolumeAudio = unavailable;
export const deleteVolumeAudio = unavailable;
export const openNodeSocket = unavailable;
