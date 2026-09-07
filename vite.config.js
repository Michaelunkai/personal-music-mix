import { defineConfig } from 'vite';
import { sites } from '@openai/sites-vite-plugin';

export default defineConfig({
  plugins: [sites()],
  build: {
    target: 'es2022',
    ssr: 'worker/index.js',
    outDir: 'dist/server',
    rollupOptions: { output: { entryFileNames: 'index.js' } },
  },
});
