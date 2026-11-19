import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

// https://vite.dev/config/
export default defineConfig({
  // The browser demo is published from docs/ at <user>.github.io/lesphotos/.
  base: process.env.VITE_DEMO ? '/lesphotos/' : '/',
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      // Photo bytes are cached ourselves in IndexedDB (see src/db.ts), not
      // via the service worker's own cache - this only needs to precache
      // the small app shell (JS/CSS/HTML) so the PWA opens and renders
      // offline. Runtime caching for API responses is deliberately not
      // configured yet; add it once the GraphQL client is in place.
      workbox: {
        globPatterns: ['**/*.{js,css,html,svg,png,ico}'],
      },
      manifest: {
        name: 'lesphotos',
        short_name: 'lesphotos',
        description: 'Self-hosted photo timeline',
        theme_color: '#111111',
        background_color: '#111111',
        display: 'standalone',
        icons: [
          {
            src: 'pwa-192x192.png',
            sizes: '192x192',
            type: 'image/png',
          },
          {
            src: 'pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png',
          },
        ],
      },
    }),
  ],
  server: {
    // Bind all interfaces so a phone on the same Wi-Fi can reach the dev
    // server at http://<mac-lan-ip>:5173. The proxy targets below stay
    // localhost deliberately: Vite proxies them server-side, from the Mac,
    // so the phone never needs to resolve or reach them itself.
    host: true,
    // Proxied rather than fetched cross-origin so the browser sees one
    // origin, which is what the samesite=lax session cookie needs. In
    // production there is no proxy at all: server/api.py serves the built
    // client itself, so it is genuinely same-origin. The old '/api' entry
    // pointing at PhotoView (:8000) is gone with PhotoView itself.
    proxy: {
      // No rewrite: the server mounts the API at /lesphotos-api itself (see
      // api:root_app, which serves the API and the built client from one
      // origin). Stripping the prefix here removed it a second time, so
      // /session 404'd and /login came back 405 - the paths landed on the
      // client-serving app instead of the API.
      '/lesphotos-api': 'http://localhost:8001',
    },
  },
  // `vite preview` (serving the production build, needed to test the real
  // service worker - it's disabled by default under `vite dev`) reads its
  // own proxy config, separate from server.proxy above. Kept identical
  // deliberately so dev and preview behave the same way.
  preview: {
    host: true,
    proxy: {
      // No rewrite: the server mounts the API at /lesphotos-api itself (see
      // api:root_app, which serves the API and the built client from one
      // origin). Stripping the prefix here removed it a second time, so
      // /session 404'd and /login came back 405 - the paths landed on the
      // client-serving app instead of the API.
      '/lesphotos-api': 'http://localhost:8001',
    },
  },
})
