import { Link, Outlet } from "@tanstack/react-router";
import { Crosshair, UserRoundSearch } from "lucide-react";

export function RootLayout() {
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-50 w-full border-b bg-background/95 backdrop-blur">
        <div className="container mx-auto flex h-14 items-center px-4">
          <Link to="/name-leads" className="mr-8 flex items-center gap-2 text-lg font-bold">
            <Crosshair className="h-5 w-5 text-primary" /><span>AI Hunter</span>
          </Link>
          <nav className="flex items-center gap-4 text-sm">
            <Link to="/name-leads" className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground">
              <UserRoundSearch className="h-4 w-4" />中文起名潜客
            </Link>
          </nav>
        </div>
      </header>
      <main className="container mx-auto px-4 py-8"><Outlet /></main>
    </div>
  );
}
