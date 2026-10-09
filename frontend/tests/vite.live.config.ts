import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({plugins:[react()],cacheDir:'/tmp/atoms-full-version-live-vite',server:{host:'0.0.0.0',port:25180,strictPort:true,cors:false,proxy:{'/api':{target:'http://127.0.0.1:38080',changeOrigin:true,ws:true}}}})
