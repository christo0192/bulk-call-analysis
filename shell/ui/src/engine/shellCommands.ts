import { invoke } from '@tauri-apps/api/core';
import { mockBridgeLoaded } from './bridge';
import { EngineError } from './client';

/** A desktop-shell command. Outside the desktop app the dev mock records it instead. */
async function shellCommand(command: string, params: Record<string, string>): Promise<null> {
  if ('__TAURI_INTERNALS__' in window) return invoke<null>(command, params);
  const mock = await mockBridgeLoaded;
  if (!mock) throw new EngineError(-32601, 'Available in the desktop app', 'method_not_allowed');
  mock.shellCalls.push({ command, params });
  return null;
}

export async function openProjectFile(project: string, name: string): Promise<null> {
  return shellCommand('open_project_file', { project, name });
}

export async function openProjectFolder(project: string): Promise<null> {
  return shellCommand('open_project_folder', { project });
}
