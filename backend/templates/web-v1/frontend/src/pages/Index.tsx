import { Button } from '@/components/ui/button';
export default function Index() {
 return <main data-atoms-starter="true" className="min-h-screen flex flex-col items-center justify-center gap-4 p-6">
  <h1 className="text-3xl font-semibold tracking-tight">Welcome to Atoms</h1>
  <p className="text-muted-foreground">在这里实现用户需求。</p>
  <Button disabled>应用开发起点</Button>
 </main>;
}
