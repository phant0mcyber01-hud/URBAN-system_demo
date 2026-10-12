export default {
  build: { target: 'esnext' },
  server: {
    proxy: {
      '/api': 'http://localhost:8080',
      '/data': 'http://localhost:8080',
      '/ws': { target: 'ws://localhost:8080', ws: true },
    },
  },
};
