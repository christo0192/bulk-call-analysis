import { useEffect, useRef } from 'preact/hooks';
import { load, settingsOpen, setupRequired } from '../state/store';
import { EngineStatusBanner } from './EngineStatusBanner';
import { Sidebar } from './Sidebar';
import { MainArea } from './MainArea';
import { SetupChecklist } from './views/SetupChecklist';
import { SettingsView } from './views/SettingsView';
import '../styles/base.css';
import '../styles/components.css';

export function App() {
  useEffect(() => {
    void load();
  }, []);
  // Settings replaces the sidebar; on the way back, focus returns to the Settings button
  // instead of being lost with the page that closed.
  const wasSettings = useRef(settingsOpen.value);
  useEffect(() => {
    if (wasSettings.current && !settingsOpen.value)
      document.querySelector<HTMLElement>('.sidebar .settings')?.focus();
    wasSettings.current = settingsOpen.value;
  }, [settingsOpen.value]);
  return (
    <>
      <>
        <EngineStatusBanner />
        <div class="window">
          {settingsOpen.value ? <SettingsView /> : <Sidebar />}
          {settingsOpen.value ? null : setupRequired.value ? <SetupChecklist /> : <MainArea />}
        </div>
      </>
    </>
  );
}
