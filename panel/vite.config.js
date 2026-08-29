import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Сборка кладёт готовые файлы прямо туда, откуда их отдаёт панель. На малину
// уезжает результат, а не исходники: Node на одноплатнике с гигабайтом памяти
// уходит в своп и падает.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../factory/panel/static',
    emptyOutDir: true,
    // Всё в один файл: панель работает без доступа наружу, и десяток мелких
    // запросов на слабом железе дороже одного крупного.
    assetsInlineLimit: 100000,
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8099' },
  },
});
