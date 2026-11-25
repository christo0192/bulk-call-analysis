import { invoke } from '@tauri-apps/api/core';
import { mockBridge } from './mockBridge';

export async function openProjectFile(project: string, name: string): Promise<null> {
  if ('__TAURI_INTERNALS__' in window) return invoke<null>('open_project_file', { project, name });
  mockBridge.shellCalls.push({ command: 'open_project_file', params: { project, name } });
  return null;
}

export async function openProjectFolder(project: string): Promise<null> {
  if ('__TAURI_INTERNALS__' in window) return invoke<null>('open_project_folder', { project });
  mockBridge.shellCalls.push({ command: 'open_project_folder', params: { project } });
  return null;
}
