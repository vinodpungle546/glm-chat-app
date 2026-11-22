"use client";

/**
 * Small shared UI building blocks (buttons, inputs, switches, callouts, badges, dialogs), styled with the
 * design tokens from globals.css so they work in light and dark theme.
 */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { AlertTriangle, CheckCircle2, Info, Loader2, X, XCircle } from "lucide-react";
import { forwardRef } from "react";
import { clsx } from "@/lib/format";

// ---------------------------------------------------------------------------
// Buttons and inputs
// ---------------------------------------------------------------------------

type Variant = "primary" | "secondary" | "ghost" | "danger" | "outline";
/** Button sizes; 'icon' sizes are square buttons holding only an icon. */
type Size = "sm" | "md" | "icon" | "icon-sm";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover shadow-sm",
  secondary: "bg-surface-2 text-fg hover:bg-hover border border-border",
  outline: "border border-border-strong text-fg hover:bg-hover",
  ghost: "text-muted hover:text-fg hover:bg-hover",
  danger: "bg-danger text-white hover:opacity-90",
};
const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-[13px] gap-1.5 rounded-lg",
  md: "h-9 px-3.5 text-sm gap-2 rounded-lg",
  icon: "h-9 w-9 rounded-lg",
  "icon-sm": "h-7 w-7 rounded-md",
};

/** A normal <button>'s props plus a style variant, a size and a loading state. */
export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

/**
 * The app's button. `loading` shows a spinner and disables it. forwardRef lets parents (e.g. Radix menus)
 * reach the real <button>.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, className, children, disabled, ...props }, ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={clsx(
        "inline-flex shrink-0 items-center justify-center font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        VARIANTS[variant], SIZES[size], className,
      )}
      {...props}
    >
      {loading && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
});

/** A styled single-line text input. */
export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className, ...props }, ref,
) {
  return (
    <input
      ref={ref}
      className={clsx(
        "h-9 w-full rounded-lg border border-border-strong bg-surface px-3 text-sm text-fg placeholder:text-faint",
        "focus:border-accent focus:outline-none focus:ring-2 focus:ring-[var(--ring)]",
        className,
      )}
      {...props}
    />
  );
});

/** A styled multi-line text input. */
export const Textarea = forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...props }, ref) {
    return (
      <textarea
        ref={ref}
        className={clsx(
          "w-full rounded-lg border border-border-strong bg-surface px-3 py-2 text-sm text-fg placeholder:text-faint",
          "focus:border-accent focus:outline-none focus:ring-2 focus:ring-[var(--ring)]",
          className,
        )}
        {...props}
      />
    );
  },
);

/** A form label, with an optional hint next to it. */
export function Label({ children, htmlFor, hint }: { children: React.ReactNode; htmlFor?: string; hint?: string }) {
  return (
    <label htmlFor={htmlFor} className="mb-1.5 block text-[13px] font-medium text-fg">
      {children}
      {hint && <span className="ml-1.5 font-normal text-faint">{hint}</span>}
    </label>
  );
}

/** An on/off switch with a label and an optional description (an accessible role='switch' button). */
export function Switch({ checked, onChange, label, description, id }: {
  checked: boolean; onChange: (v: boolean) => void; label: string; description?: React.ReactNode; id: string;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <div>
        <label htmlFor={id} className="text-sm font-medium">{label}</label>
        {description && <div className="mt-0.5 text-[13px] text-muted">{description}</div>}
      </div>
      <button
        id={id}
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={clsx("relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors",
          checked ? "bg-accent" : "bg-border-strong")}
      >
        <span className={clsx("absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform",
          checked ? "translate-x-[18px]" : "translate-x-0.5")} />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Feedback
// ---------------------------------------------------------------------------

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={clsx("h-4 w-4 animate-spin text-muted", className)} aria-label="Loading" />;
}

/** A progress bar; `value` is 0-100, `label` is read by screen readers. */
export function Progress({ value, className, label }: { value: number; className?: string; label?: string }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className={clsx("h-1.5 w-full overflow-hidden rounded-full bg-surface-2", className)}
         role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <div className="h-full rounded-full bg-accent transition-[width] duration-500" style={{ width: `${pct}%` }} />
    </div>
  );
}

/** The colour/meaning of a callout. */
type Tone = "info" | "warning" | "danger" | "success";
const TONES: Record<Tone, { box: string; icon: React.ReactNode }> = {
  info: { box: "border-accent/25 bg-accent-soft", icon: <Info className="h-4 w-4 text-accent" /> },
  warning: { box: "border-warning/30 bg-warning-soft", icon: <AlertTriangle className="h-4 w-4 text-warning" /> },
  danger: { box: "border-danger/30 bg-danger-soft", icon: <XCircle className="h-4 w-4 text-danger" /> },
  success: { box: "border-success/30 bg-success-soft", icon: <CheckCircle2 className="h-4 w-4 text-success" /> },
};

/** A coloured message box (info, warning, danger, success) with an optional title and action button. */
export function Callout({ tone = "info", title, children, action, className, role, ...rest }: {
  tone?: Tone; title?: React.ReactNode; children?: React.ReactNode; action?: React.ReactNode; className?: string;
  role?: string; "data-testid"?: string;
}) {
  return (
    <div role={role ?? (tone === "danger" ? "alert" : "status")} data-testid={rest["data-testid"]}
         className={clsx("flex gap-3 rounded-xl border px-3.5 py-3 text-sm", TONES[tone].box, className)}>
      <div className="mt-0.5 shrink-0">{TONES[tone].icon}</div>
      <div className="min-w-0 flex-1">
        {title && <div className="font-medium">{title}</div>}
        {children && <div className={clsx("text-[13px] leading-relaxed", title ? "mt-0.5 text-muted" : "text-fg")}>{children}</div>}
        {action && <div className="mt-2.5 flex flex-wrap gap-2">{action}</div>}
      </div>
    </div>
  );
}

/** A small coloured label (status, counts). */
export function Badge({ tone = "neutral", children, className, title }: {
  tone?: "neutral" | "success" | "warning" | "danger" | "accent"; children: React.ReactNode; className?: string;
  title?: string;
}) {
  const tones = {
    neutral: "bg-surface-2 text-muted border-border",
    success: "bg-success-soft text-success border-success/20",
    warning: "bg-warning-soft text-warning border-warning/25",
    danger: "bg-danger-soft text-danger border-danger/25",
    accent: "bg-accent-soft text-accent border-accent/20",
  };
  return (
    <span title={title} className={clsx("inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4", tones[tone], className)}>
      {children}
    </span>
  );
}

/** What a list shows when it has nothing yet. */
export function EmptyState({ icon, title, children, action }: {
  icon: React.ReactNode; title: string; children?: React.ReactNode; action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2 text-muted">{icon}</div>
      <h3 className="text-sm font-semibold">{title}</h3>
      {children && <p className="mt-1 max-w-sm text-[13px] text-muted">{children}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/** A keyboard key, e.g. ⌘ or K. */
export function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-border-strong bg-surface-2 px-1 font-mono text-[11px] text-muted">
      {children}
    </kbd>
  );
}

// ---------------------------------------------------------------------------
// Overlays
// ---------------------------------------------------------------------------

export function Tooltip({ content, children, side = "top" }: {
  content: React.ReactNode; children: React.ReactNode; side?: "top" | "right" | "bottom" | "left";
}) {
  return (
    <TooltipPrimitive.Root delayDuration={300}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content side={side} sideOffset={6}
          className="z-[70] max-w-xs rounded-md bg-fg px-2 py-1 text-xs text-bg shadow-md">
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

/**
 * A modal dialog (Radix): closes with Esc or a click outside, traps focus, and has a title for screen
 * readers.
 */
export function Dialog({ open, onOpenChange, title, description, children, className, footer, hideTitle }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: string; description?: React.ReactNode;
  children?: React.ReactNode; className?: string; footer?: React.ReactNode; hideTitle?: boolean;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[2px]" />
        <DialogPrimitive.Content
          aria-describedby={description ? undefined : undefined}
          className={clsx("fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2",
            "rounded-2xl border border-border bg-surface shadow-card focus:outline-none", className)}
        >
          <div className={clsx("flex items-start justify-between gap-4 px-5 pt-5", hideTitle && "sr-only")}>
            <div>
              <DialogPrimitive.Title className="text-base font-semibold">{title}</DialogPrimitive.Title>
              {description ? (
                <DialogPrimitive.Description className="mt-1 text-[13px] text-muted">{description}</DialogPrimitive.Description>
              ) : (
                <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
              )}
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon-sm" aria-label="Close"><X className="h-4 w-4" /></Button>
            </DialogPrimitive.Close>
          </div>
          {children && <div className="px-5 py-4">{children}</div>}
          {footer && <div className="flex justify-end gap-2 border-t border-border px-5 py-3">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** 'Are you sure?' dialog with Cancel and a (red by default) confirm button. */
export function ConfirmDialog({ open, onOpenChange, title, description, confirmLabel = "Delete", onConfirm, danger = true, children }: {
  open: boolean; onOpenChange: (o: boolean) => void; title: string; description?: React.ReactNode;
  confirmLabel?: string; onConfirm: () => void; danger?: boolean; children?: React.ReactNode;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={title} description={description}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant={danger ? "danger" : "primary"} onClick={() => { onConfirm(); onOpenChange(false); }}>
            {confirmLabel}
          </Button>
        </>
      }>
      {children}
    </Dialog>
  );
}
