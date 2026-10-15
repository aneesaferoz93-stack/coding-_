import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';
import { detectLang, setLang } from './i18n';

setLang(detectLang());

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// Offline shell. The app must open with no signal at all — that is the entire
// point for a Patwari standing in a field in Upper Dir.
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((e) => console.warn('sw failed', e));
  });
}
