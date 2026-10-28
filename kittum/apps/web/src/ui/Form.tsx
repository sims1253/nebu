// Forms for the lead's setup steps: labelled fields, the two button weights, file pickers, and
// the dialog that holds a form. A dialog blocks screen keys while it is open (ui/keys.ts).
import { Dialog } from "@base-ui/react/dialog";
import type { ComponentProps, ReactNode } from "react";

import { WorkbenchError } from "../data/workbench.ts";
import { Key } from "./Key.tsx";

const control =
  "w-full rounded-[3px] border border-rule-strong bg-sheet px-2.5 py-1.5 text-sm text-ink placeholder:text-ink-3 focus-visible:border-petrol focus-visible:outline-none disabled:opacity-60";

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="grid gap-1">
      <span className="text-xs font-semibold text-ink-2">{label}</span>
      {children}
      {hint ? <span className="text-xs text-ink-3">{hint}</span> : null}
    </label>
  );
}

export const TextInput = (props: ComponentProps<"input">) => (
  <input type="text" {...props} className={`${control} ${props.className ?? ""}`} />
);

export const TextArea = (props: ComponentProps<"textarea">) => (
  <textarea {...props} className={`${control} ${props.className ?? ""}`} />
);

export const Select = (props: ComponentProps<"select">) => (
  <select {...props} className={`${control} ${props.className ?? ""}`} />
);

/** A file picker that hands over the chosen file; `accept` limits what the browser offers. */
export function FileInput({
  accept,
  onFile,
  label,
}: {
  accept?: string;
  onFile: (file: File | null) => void;
  label: string;
}) {
  return (
    <input
      type="file"
      aria-label={label}
      accept={accept}
      onChange={(e) => onFile(e.currentTarget.files?.[0] ?? null)}
      className="block w-full text-sm text-ink-2 file:mr-3 file:rounded-[3px] file:border file:border-rule-strong file:bg-sheet-2 file:px-2.5 file:py-1 file:text-sm file:text-ink hover:file:bg-sheet"
    />
  );
}

export function Button({
  primary,
  keys,
  children,
  ...props
}: ComponentProps<"button"> & { primary?: boolean; keys?: string }) {
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex items-center gap-2 rounded-[3px] px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${
        primary
          ? "bg-petrol text-petrol-ink hover:brightness-110"
          : "border border-rule-strong bg-sheet text-ink hover:bg-sheet-2"
      } ${props.className ?? ""}`}
    >
      {children}
      {keys ? <Key k={keys} /> : null}
    </button>
  );
}

/** A form in a dialog. `onSubmit` runs on the submit button and on Enter in a single-line field. */
export function FormDialog({
  open,
  onOpenChange,
  title,
  description,
  submit,
  busy,
  error,
  onSubmit,
  children,
  wide,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  submit: string;
  busy?: boolean;
  error?: string | null;
  onSubmit: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 bg-[rgb(10_20_24/0.32)]" />
        <Dialog.Popup
          className={`fixed top-16 left-1/2 max-h-[calc(100vh-8rem)] -translate-x-1/2 overflow-y-auto rounded-[4px] border border-rule-strong bg-sheet p-6 shadow-[0_16px_40px_rgb(0_0_0/0.2)] outline-none ${
            wide ? "w-[720px]" : "w-[520px]"
          }`}
        >
          <Dialog.Title className="text-lg font-semibold">{title}</Dialog.Title>
          {description ? (
            <Dialog.Description className="mt-1 max-w-[62ch] text-sm text-ink-2">
              {description}
            </Dialog.Description>
          ) : null}
          <form
            className="mt-5 grid gap-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (!busy) onSubmit();
            }}
          >
            {children}
            {error ? (
              <p role="alert" className="text-sm text-exclude" data-testid="form-error">
                {error}
              </p>
            ) : null}
            <div className="flex items-center gap-3 pt-1">
              <Button primary type="submit" disabled={busy}>
                {busy ? "Working" : submit}
              </Button>
              <Dialog.Close className="rounded-[3px] px-3 py-1.5 text-sm text-ink-2 hover:text-ink">
                Cancel
              </Dialog.Close>
            </div>
          </form>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** The error text of a failed call, in the server's words where it gave them. */
export const failure = (e: unknown, fallback: string) =>
  e instanceof WorkbenchError && e.message ? e.message : fallback;
