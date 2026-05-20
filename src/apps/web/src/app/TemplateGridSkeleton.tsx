export default function TemplateGridSkeleton() {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
      {Array.from({ length: 8 }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-zinc-900 overflow-hidden"
        >
          <div className="aspect-[9/16] bg-zinc-900 animate-pulse" />
          <div className="p-4 space-y-2">
            <div className="h-4 bg-zinc-900 rounded animate-pulse w-2/3" />
            <div className="h-3 bg-zinc-900 rounded animate-pulse w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}
