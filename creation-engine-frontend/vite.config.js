import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import AutoImport from 'unplugin-auto-import/vite'
import { AntDesignVueResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [
    vue(),
    Components({
      resolvers: [
        AntDesignVueResolver({
          importStyle: false,
        }),
      ],
    }),
    AutoImport({
      imports: ['vue', 'vue-router'],
      dts: false,
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5174,
    open: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
      // 迁移过来的创作代码有一部分请求原本直连 CMS 管理后台。开发期直连方便联调;
      // 生产由 creation-engine 后端代理,不让浏览器持有 CMS 令牌。
      '/sys-api': {
        target: 'https://testapi.farmmx.com/admin/',
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/sys-api/, ''),
      },
    },
  },
})
