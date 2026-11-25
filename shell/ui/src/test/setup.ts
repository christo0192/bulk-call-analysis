import '@testing-library/jest-dom';
import { mockBridgeLoaded } from '../engine/bridge';

// Outside Tauri the bridge loads the mock with a dynamic import; let it finish before any test,
// so a listener a component adds is attached at once, as it is in the app.
await mockBridgeLoaded;
