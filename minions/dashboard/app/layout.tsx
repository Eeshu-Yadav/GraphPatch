import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Minions Dashboard",
  description: "Blueprint engine metrics and task management",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased dark`}
    >
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <header className="border-b border-border bg-card">
          <div className="mx-auto max-w-7xl px-6 py-4 flex items-center justify-between">
            <div className="flex items-center gap-6">
              <Link href="/" className="text-lg font-semibold tracking-tight">
                Minions
              </Link>
              <nav className="flex gap-4 text-sm text-muted-foreground">
                <Link href="/" className="hover:text-foreground transition-colors">
                  Overview
                </Link>
                <Link href="/tasks" className="hover:text-foreground transition-colors">
                  Tasks
                </Link>
              </nav>
            </div>
            <div className="text-xs text-muted-foreground font-mono">
              Blueprint Engine
            </div>
          </div>
        </header>
        <main className="flex-1 mx-auto max-w-7xl w-full px-6 py-8">
          {children}
        </main>
      </body>
    </html>
  );
}
