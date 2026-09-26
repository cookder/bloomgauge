import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/postcss';
import { fileURLToPath } from 'node:url';
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': fileURLToPath(new URL('.', import.meta.url)) } },
  css: { postcss: { plugins: [tailwindcss()] } },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (outgoing, incoming) => {
            if (
              ['http://127.0.0.1:5173', 'http://localhost:5173'].includes(
                incoming.headers.origin ?? '',
              )
            )
              outgoing.setHeader('Origin', 'http://127.0.0.1:8765');
          });
        },
      },
    },
  },
  build: { outDir: 'dist/local' },
});
