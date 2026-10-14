// The deployed image does not run browser test fixtures. Keep its lock contract
// unchanged when adding this isolated, dependency-free IndexedDB test package.
export function excludeIndexedDbFixture(manifest, lock) {
  if (manifest.dependencies?.['fake-indexeddb']) throw new Error('IndexedDB fixture must remain test-only');
  const version = manifest.devDependencies?.['fake-indexeddb'];
  if (!version) return {manifest, lock};
  if (version !== '6.2.4') throw new Error('Review IndexedDB fixture deployment exclusion after version changes');
  const blocks = [
    '      fake-indexeddb:\n        specifier: 6.2.4\n        version: 6.2.4\n',
    '  fake-indexeddb@6.2.4:\n    resolution: {integrity: sha512-INKeIKEtSViN4yVtEWEUqbsqmaIy7Ls+MfU0yxQVXg67pOJ/sH1ZxcVrP8XrKULUFohcPD9gnmym+qBfEybACw==}\n    engines: {node: \'\u003e=18\'}\n\n',
    '  fake-indexeddb@6.2.4: {}\n\n',
  ];
  let runtimeLock = lock.replaceAll('\r\n', '\n');
  for (const block of blocks) {
    if (runtimeLock.split(block).length !== 2) throw new Error('Unexpected IndexedDB fixture lock shape');
    runtimeLock = runtimeLock.replace(block, '');
  }
  if (runtimeLock.includes('fake-indexeddb')) throw new Error('IndexedDB fixture has additional dependency references');
  const runtimeManifest = structuredClone(manifest);
  delete runtimeManifest.devDependencies['fake-indexeddb'];
  return {manifest: runtimeManifest, lock: runtimeLock};
}
