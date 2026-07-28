/**
 * The renderer's entire privileged surface. Context isolation is on and node
 * integration is off, so this list is exhaustive by construction: the timeline
 * UI can read and write hand-authored blocks and learn which cloud to talk to,
 * and nothing else.
 */
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('workOverview', {
  listBlocks: () => ipcRenderer.invoke('sessions:list'),
  saveBlock: (block: unknown) => ipcRenderer.invoke('sessions:save', block),
  deleteBlock: (id: string) => ipcRenderer.invoke('sessions:delete', id),
  cloudConfig: () => ipcRenderer.invoke('cloud:config'),
})
