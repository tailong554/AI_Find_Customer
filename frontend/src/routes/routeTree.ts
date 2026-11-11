import { createRootRoute, createRoute, Navigate } from "@tanstack/react-router";
import { RootLayout } from "./root";
import { NameLeadsPage } from "./name-leads";
import { PublicNameRequestPage } from "./public-name-request";

const rootRoute = createRootRoute({ component: RootLayout });
const leadsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: NameLeadsPage });
const publicRequestRoute = createRoute({ getParentRoute: () => rootRoute, path: "/name-request", component: PublicNameRequestPage });
const namedLeadsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/name-leads", component: NameLeadsPage });
export const routeTree = rootRoute.addChildren([leadsRoute, namedLeadsRoute, publicRequestRoute]);
