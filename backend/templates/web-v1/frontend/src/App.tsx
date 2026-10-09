import { BrowserRouter, Route, Routes } from 'react-router-dom';
import Index from '@/pages/Index';
// Live Vite preview has an absolute BASE_URL. Static publishing injects <base>
// at its /api/public/.../ mount; a standalone deployment stays at the root.
const base = import.meta.env.BASE_URL;
const mount = base.startsWith('/') ? base : new URL(document.querySelector('base')?.href || '/', window.location.origin).pathname;
export default function App() {
 return <BrowserRouter basename={mount.replace(/\/$/, '') || '/'}>
  <Routes><Route path="/" element={<Index />} /></Routes>
 </BrowserRouter>;
}
