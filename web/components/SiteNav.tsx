"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Home" },
  { href: "/onboard", label: "Onboard" },
  { href: "/live", label: "Live" },
  { href: "/catalog", label: "Catalog" },
  { href: "/approvals", label: "Approvals" },
  { href: "/metrics", label: "Metrics" },
] as const;

export function SiteNav() {
  const pathname = usePathname();

  return (
    <header className="border-b border-rule bg-paper">
      <nav className="mx-auto max-w-6xl px-4 sm:px-6 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-2.5">
        <Link href="/" className="font-display text-lg tracking-tight">
          PRAMAN
        </Link>
        <ul className="flex flex-wrap items-center gap-x-0.5 gap-y-1">
          {LINKS.map((link) => {
            const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <li key={link.href}>
                <Link
                  href={link.href}
                  className={`px-2.5 py-1.5 text-sm font-mono uppercase tracking-wide whitespace-nowrap transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-ink ${
                    active ? "text-ink border-b-2 border-ink" : "text-ink-muted hover:text-ink"
                  }`}
                  aria-current={active ? "page" : undefined}
                >
                  {link.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </header>
  );
}
