import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => ({
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api': {
      target: loadEnv(mode, '.', '').PRADA_API_PROXY_TARGET || 'http://127.0.0.1:8000', ws: true,
      configure(proxy) {
        const websocketProxy = proxy as unknown as { on: (
          event: 'proxyReqWs', listener: (
            request: { setHeader: (name: string, value: string) => void },
            client: { headers: { origin?: string; host?: string } },
          ) => void,
        ) => void };
        websocketProxy.on('proxyReqWs', (request, client) => {
          const origin = client.headers.origin;
          const host = client.headers.host;
          if (!origin || !host) return;
          try {
            const url = new URL(origin);
            if (url.host === host && url.protocol === 'http:' &&
                /^(?:localhost|127\.0\.0\.1|10\.[\d.]+|192\.168\.[\d.]+|172\.(?:1[6-9]|2\d|3[01])\.[\d.]+)$/.test(url.hostname))
              request.setHeader('Origin', 'http://127.0.0.1:5173');
          } catch {}
        });
      },
    } },
  },
}));
