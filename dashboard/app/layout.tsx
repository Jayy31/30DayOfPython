import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Papertrade",
  description: "Paper-trading dashboard for Nifty/Sensex index options (no live orders).",
};

const NAV = [
  ["/", "Today"],
  ["/trades", "Trades"],
  ["/metrics", "Metrics"],
  ["/shadow", "Shadow rules"],
  ["/reports", "Reports"],
] as const;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <div className="inner">
            <span className="brand">Papertrade</span>
            <nav>
              {NAV.map(([href, label]) => (
                <Link key={href} href={href}>
                  {label}
                </Link>
              ))}
            </nav>
            <span className="badge paper" title="Simulated fills only. No broker order API exists in this system.">
              Paper trading only
            </span>
          </div>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
