/**
 * Work Overview — Electron main process.
 *
 * Two responsibilities beyond opening the window:
 *   - own the work-session store, so sessions survive independently of the
 *     cloud and of the Library Tool sidecar (the renderer never touches disk);
 *   - hold the Supabase credentials, so the renderer is handed a URL and an
 *     anon key rather than reading any secret store itself.
 *
 * The window is expected to be viewed maximized, so it opens that way and the
 * renderer lays out for the full screen.
 */
import { app, BrowserWindow, ipcMain, shell } from 'electron'
import path from 'node:path'

import { SessionStore } from './sessionStore.js'
import { loadCloudConfig } from './cloudConfig.js'

// This file is bundled to CommonJS (Electron's preload must be CJS, and the
// main process matches it). `import.meta.url` is empty under that format, so
// the bundle's own __dirname is the only correct way to locate sibling files.
declare const __dirname: string
const here = __dirname
const devUrl = process.env.WORK_OVERVIEW_DEV_URL

// Dark to the frame, not just the page: a light title bar around a dark app is
// the tell that gives away a themed web view.
const CHROME = '#0b0c0e'

let store: SessionStore

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1600,
    height: 980,
    minWidth: 900,
    minHeight: 600,
    show: false,
    backgroundColor: CHROME,
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(here, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  // Maximize before showing: presenting a small window and then snapping it
  // large is visible flicker on Windows.
  win.maximize()
  win.once('ready-to-show', () => win.show())

  // Anything that is not this app opens in the real browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })

  if (devUrl) {
    void win.loadURL(devUrl)
  } else {
    void win.loadFile(path.join(here, '..', 'dist', 'index.html'))
  }
}

app.whenReady().then(() => {
  store = new SessionStore(path.join(app.getPath('userData'), 'work_sessions.json'))

  ipcMain.handle('sessions:list', () => store.list())
  ipcMain.handle('sessions:save', (_e, block: unknown) => store.save(block))
  ipcMain.handle('sessions:delete', (_e, id: unknown) => store.remove(String(id)))
  ipcMain.handle('cloud:config', () => loadCloudConfig())

  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
