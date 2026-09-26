import React from 'react';
import { createRoot } from 'react-dom/client';
import Dashboard from './app/page';
import { FirstLaunch } from './components/dashboard/first-launch';
import { SupportReporting } from './components/dashboard/support-reporting';
import { ScreenErrorBoundary } from './components/dashboard/screen-error';
import './app/globals.css';
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <SupportReporting>
      <ScreenErrorBoundary name="application">
        <FirstLaunch>
          <Dashboard />
        </FirstLaunch>
      </ScreenErrorBoundary>
    </SupportReporting>
  </React.StrictMode>,
);
