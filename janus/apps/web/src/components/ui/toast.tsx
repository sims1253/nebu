import { create } from "zustand";
import { X } from "lucide-react";

interface Toast {
  id: number;
  title: string;
  description?: string;
}

const useToastStore = create<{ toasts: Toast[] }>(() => ({ toasts: [] }));
let nextId = 1;

function dismiss(id: number) {
  useToastStore.setState((state) => ({
    toasts: state.toasts.filter((item) => item.id !== id),
  }));
}

export const toast = {
  error(title: string, options?: { description?: string }) {
    const id = nextId++;
    useToastStore.setState((state) => ({
      toasts: [...state.toasts.slice(-2), { id, title, ...options }],
    }));
    setTimeout(() => dismiss(id), 8000);
  },
};

export function Toaster() {
  const toasts = useToastStore((state) => state.toasts);
  return (
    <div
      className="fixed bottom-3 right-3 z-[60] flex w-80 flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      {toasts.map((item) => (
        <div
          key={item.id}
          className="pointer-events-auto flex items-start gap-2 rounded-md border border-destructive/40 bg-background p-2.5 text-xs text-destructive-text shadow-md animate-toast-in motion-reduce:animate-none"
        >
          <div className="min-w-0 flex-1 space-y-0.5">
            <p className="font-medium leading-snug">{item.title}</p>
            {item.description && (
              <p className="text-muted-foreground leading-snug">
                {item.description}
              </p>
            )}
          </div>
          <button
            type="button"
            aria-label="Dismiss notification"
            className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => dismiss(item.id)}
          >
            <X className="size-3.5" aria-hidden="true" />
          </button>
        </div>
      ))}
    </div>
  );
}
