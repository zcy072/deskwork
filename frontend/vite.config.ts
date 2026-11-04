import vue from '@vitejs/plugin-vue'
import { NaiveUiResolver } from 'unplugin-vue-components/resolvers'
import Components from 'unplugin-vue-components/vite'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue(), Components({ resolvers: [NaiveUiResolver()], dts: false })],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: {
    rolldownOptions: {
      output: {
        // 依赖按更新频率拆开,业务代码改动时 vendor 缓存仍然有效
        codeSplitting: {
          groups: [
            { name: 'vue', test: /node_modules[\\/](vue|@vue|pinia)[\\/]/, priority: 30 },
            { name: 'markdown', test: /node_modules[\\/](marked|dompurify)[\\/]/, priority: 20 },
            { name: 'naive-ui', test: /node_modules[\\/](naive-ui|vueuc|vooks|vdirs|evtd|seemly|treemate|css-render|@css-render|date-fns|lodash-es|async-validator|@emotion|csstype)[\\/]/, priority: 10 },
          ],
        },
      },
    },
  },
  test: { environment: 'jsdom' },
})
