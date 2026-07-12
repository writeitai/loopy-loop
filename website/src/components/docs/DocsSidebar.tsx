"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import { docsNavigation, type NavItem } from "@/lib/docs/navigation";

function isActive(pathname: string, href: string) {
  const norm = (p: string) => (p.length > 1 ? p.replace(/\/$/, "") : p);
  return norm(pathname) === norm(href);
}

function NavLink({ item }: { item: NavItem }) {
  const pathname = usePathname();
  const active = isActive(pathname, item.href);

  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "block rounded-md px-3 py-1.5 text-sm transition-colors",
        active
          ? "bg-accent font-medium text-accent-foreground"
          : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
      )}
    >
      {item.title}
    </Link>
  );
}

function NavSection({ item }: { item: NavItem }) {
  const pathname = usePathname();
  const norm = (p: string) => (p.length > 1 ? p.replace(/\/$/, "") : p);
  const parentActive =
    isActive(pathname, item.href) ||
    norm(pathname).startsWith(norm(item.href) + "/") ||
    (item.children?.some((c) => isActive(pathname, c.href)) ?? false);

  return (
    <div>
      <p className="px-3 py-1.5 text-sm font-semibold text-foreground">
        {item.title}
      </p>
      {item.children && (
        <div className="ml-3 space-y-0.5 border-l border-border pl-3">
          {item.children.map((child) => (
            <NavLink key={child.href} item={child} />
          ))}
        </div>
      )}
      {!parentActive && null}
    </div>
  );
}

export function DocsSidebar() {
  return (
    <nav className="space-y-1" aria-label="Documentation">
      {docsNavigation.map((item) =>
        item.children ? (
          <NavSection key={item.href} item={item} />
        ) : (
          <NavLink key={item.href} item={item} />
        )
      )}
    </nav>
  );
}
