"use client";

/** Wraps the app in its context providers: theme (light/dark), toasts and the app state. */
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { ThemeProvider, useTheme } from "next-themes";
import { Toaster } from "sonner";
import { AppProvider } from "@/components/app-provider";

/** Toast notifications that follow the current theme. */
function ThemedToaster() {
  const { resolvedTheme } = useTheme();
  return (
    <Toaster position="bottom-right" theme={resolvedTheme === "dark" ? "dark" : "light"} closeButton
             toastOptions={{ classNames: { toast: "!rounded-xl !border-border !shadow-card" } }} />
  );
}

/** The provider stack used by the root layout. */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      <TooltipPrimitive.Provider>
        <AppProvider>
          {children}
          <ThemedToaster />
        </AppProvider>
      </TooltipPrimitive.Provider>
    </ThemeProvider>
  );
}
