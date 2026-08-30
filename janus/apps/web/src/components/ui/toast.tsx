import { create } from "zustand";
import { X } from "lucide-react";
import { cn } from "~/lib/utils";

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface Toast {
  id: number;
  title: string;
  description?: string;
  variant: "default" | "success" | "destructive";
  action?: ToastAction;
  /** Auto-dismiss delay in ms. */
  duration: number;
}

interface ToastState {
  toasts: Toast[];
  show: (
    toast: Omit<Toast, "id" | "duration"> & { duration?: number },
  ) => number;
  dismiss: (id: number) => void;
}

let nextToastId = 1;
const timers = new Map<number, ReturnType<typeof setTimeout>>();

export const useToastStore = create<ToastState>((set, get) => ({
  toasts: [],
  show: (toast) => {
    const id = nextToastId++;
    const duration =
      toast.duration ?? (toast.variant === "destructive" ? 8000 : 5000);
    set((state) => ({
      // Keep at most 3 toasts on screen; drop the oldest first
      toasts: [...state.toasts.slice(-2), { ...toast, id, duration }],
    }));
    timers.set(
      id,
      setTimeout(() => get().dismiss(id), duration),
    );
    return id;
  },
  dismiss: (id) => {
    const timer = timers.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.delete(id);
    }
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }));
  },
}));

/** Imperative helper for use outside React components (e.g. mutation callbacks). */
export const toast = {
  show: (t: Omit<Toast, "id" | "duration"> & { duration?: number }) =>
    useToastStore.getState().show(t),
  success: (
    title: string,
    opts?: { description?: string; action?: ToastAction; duration?: number },
  ) => useToastStore.getState().show({ title, variant: "success", ...opts }),
  error: (
    title: string,
    opts?: { description?: string; action?: ToastAction; duration?: number },
  ) =>
    useToastStore.getState().show({ title, variant: "destructive", ...opts }),
  dismiss: (id: number) => useToastStore.getState().dismiss(id),
};

// ---------------------------------------------------------------------------
// Toaster component
// ---------------------------------------------------------------------------

const VARIANT_CLASSES: Record<Toast["variant"], string> = {
  default: "border-border bg-background text-foreground",
  success: "border-success/40 bg-background text-foreground",
  destructive: "border-destructive/40 bg-destructive/5 text-destructive-text",
};

export function Toaster() {
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);

  if (toasts.length === 0) return null;

  return (
    <div
      className="fixed bottom-3 right-3 z-[60] flex w-80 flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            "pointer-events-auto flex items-start gap-2 rounded-md border p-2.5 text-xs shadow-md",
            "animate-toast-in motion-reduce:animate-none",
            VARIANT_CLASSES[t.variant],
          )}
        >
          <div className="min-w-0 flex-1 space-y-0.5">
            <p className="font-medium leading-snug">{t.title}</p>
            {t.description && (
              <p className="text-muted-foreground leading-snug">
                {t.description}
              </p>
            )}
          </div>
          {t.action && (
            <button
              type="button"
              className={cn(
                "shrink-0 rounded border px-2 py-1 text-xs font-semibold transition-colors",
                "bg-background hover:bg-muted",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              )}
              onClick={() => {
                t.action!.onClick();
                dismiss(t.id);
              }}
            >
              {t.action.label}
            </button>
          )}
          <button
            type="button"
            aria-label="Dismiss notification"
            className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => dismiss(t.id)}
          >
            <X className="size-3.5" aria-hidden="true" />
          </button>
        </div>
      ))}
    </div>
  );
}
