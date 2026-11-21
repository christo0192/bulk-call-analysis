import { useEffect } from 'preact/hooks';
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
