import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Moon, Sun } from "lucide-react";
import { useHealth } from "~/lib/queries";
import { useTheme } from "~/hooks/useTheme";
import { Button } from "~/components/ui/button";

export function AppShell({ children }: { children: ReactNode }) {
  const health = useHealth();
  const { theme, toggleTheme } = useTheme();
  return (
    <div className="flex h-dvh flex-col bg-background text-sm">
      <header className="app-header">
        <Link to="/" className="flex items-baseline gap-2">
          <span className="font-semibold">Janus</span>
          <span className="text-[10px] text-muted-foreground">
            PDF extraction
          </span>
        </Link>
        <div className="flex items-center gap-2">
          {health.isError && (
            <span className="text-xs text-destructive">Backend offline</span>
          )}
          {health.isSuccess && health.data?.status !== "healthy" && (
            <span className="text-xs text-destructive">Backend unhealthy</span>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={toggleTheme}
            aria-label="Toggle theme"
          >
            {theme === "dark" ? (
              <Sun className="size-4" />
            ) : (
              <Moon className="size-4" />
            )}
          </Button>
          <Link to="/compare" className="app-nav-link">
            Comparison reviews
          </Link>
          <Link to="/" className="app-nav-link">
            New extraction
          </Link>
        </div>
      </header>
      <main className="min-h-0 flex-1 overflow-auto">{children}</main>
    </div>
  );
}
