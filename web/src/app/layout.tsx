/**
 * The root layout of the Next.js app: fonts, global CSS, the theme and app providers, and the shell
 * (sidebar + top bar) around every page.
 */
import type { Metadata, Viewport } from "next";
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";
import { AppShell } from "@/components/app-shell";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Quorum", template: "%s · Quorum" },
  description: "Quorum — private meeting intelligence: offline transcription, speaker identification and "
    + "meeting summaries with local or cloud models.",
  applicationName: "Quorum",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#1a1a1a" },
  ],
};

/** Wraps every page: <html>/<body>, Providers (theme, toasts, app state) and AppShell. */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body className="h-full overflow-hidden">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
