import { useEffect } from 'preact/hooks';
import { load, settingsOpen, setupItems, setupRequired } from '../state/store';
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
  const checklistPending = setupItems.value.some(
    (item) => item.status !== 'ok' && item.status !== 'manual_done',
  );
  return (
    <>
      <EngineStatusBanner />
      <div class="window">
        {settingsOpen.value ? <SettingsView /> : <Sidebar />}
        {settingsOpen.value ? null : setupRequired.value || checklistPending ? (
          <SetupChecklist />
        ) : (
          <MainArea />
        )}
      </div>
    </>
  );
}
