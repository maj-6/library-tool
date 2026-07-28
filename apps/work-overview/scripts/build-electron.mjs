/**
 * Bundle the main process and preload with esbuild.
 *
 * Both emit CommonJS: Electron's preload must be CJS, and keeping main.cjs the
 * same format avoids the ESM-in-Electron footguns for no benefit here. The
 * cloud defaults are regenerated first so a build can never ship stale ones.
 */
import { build } from 'esbuild'
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const appDir = path.resolve(here, '..')

execFileSync(process.execPath, [path.join(here, 'gen-cloud-defaults.mjs')], { stdio: 'inherit' })

const common = {
  bundle: true,
  platform: 'node',
  target: 'node22',
  format: 'cjs',
  external: ['electron'],
  sourcemap: true,
  logLevel: 'info',
}

await build({
  ...common,
  entryPoints: [path.join(appDir, 'electron', 'main.ts')],
  outfile: path.join(appDir, 'dist-electron', 'main.cjs'),
})

await build({
  ...common,
  entryPoints: [path.join(appDir, 'electron', 'preload.cts')],
  outfile: path.join(appDir, 'dist-electron', 'preload.cjs'),
})
